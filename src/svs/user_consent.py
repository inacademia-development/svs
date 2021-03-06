import gettext
import json
from urllib.parse import urlparse

import pkg_resources
from mako.lookup import TemplateLookup
from mako.template import Template
from satosa.exception import SATOSAAuthenticationError
from satosa.internal_data import InternalResponse
from satosa.micro_services.base import ResponseMicroService
from satosa.response import Response, Redirect
from satosa.logging_util import satosa_logging
from .util.transaction_flow_logging import transaction_log
from .error_description import ErrorDescription, ERROR_DESC, LOG_MSG

import logging
logger = logging.getLogger('satosa')

STATE_KEY = "CONSENT"

def N_(s):
    """
    Dummy function to mark strings for translation, but defer the actual translation for later (using the real "_()").
    :param s:
    :return:
    """
    return s


class UserConsent(ResponseMicroService):

    def __init__(self, config, *args, **kwargs):
        """
        Constructor.
        """
        super().__init__(*args, **kwargs)
        self.config = config
        self.logo_base_path = config['logo_base_path']
        self.privacy_url = config.get('privacy_url', "https://inacademia.org/privacy-and-data-protection/")
        self.attributes = config.get('attributes', {})
        self.endpoint = '/handle_consent'
        self.template_lookup = TemplateLookup(directories=[pkg_resources.resource_filename('svs', 'templates/')])
        log_target = config.get('log_target', 'consent.log')
        self.loghandle = open(log_target,"a")
        satosa_logging(logger, logging.INFO, "UserConsent micro_service is active", None)


    def _find_requester_name(self, requester_name, language):
        return requester_name
        requester_names = {entry['lang']: entry['text'] for entry in requester_name}
        # fallback to english, or if all else fails, use the first entry in the list of names
        fallback = requester_names.get('en', requester_name[0]['text'])
        return requester_names.get(language, fallback)

    def _attributes_to_release(self, internal_response):
        attributes = {}
        for attribute, name in self.attributes.items():
            value = internal_response.attributes.get(attribute, None)
            if value:
                attributes[N_(name)] = value
        return attributes

    def render_consent(self, consent_state, internal_response, language='en'):
        requester_name = consent_state.get('requester_display_name', None)
        if not requester_name:
            requester_name = self._find_requester_name(internal_response.requester, language)
        requester_logo = consent_state.get('requester_logo', None)
        gettext.translation('messages', localedir=pkg_resources.resource_filename('svs', 'data/i18n/locale'),
                            languages=[language]).install()

        released_attributes = self._attributes_to_release(internal_response)
        template = self.template_lookup.get_template('consent.mako')
        page = template.render(requester_name=requester_name,
                               requester_logo=self._normalize_logo(requester_logo),
                               privacy_url=self.privacy_url,
                               released_claims=released_attributes,
                               form_action='/consent{}'.format(self.endpoint),
                               language=language)
        page = Template(page).render(requester_name=requester_name,
                               requester_logo=self._normalize_logo(requester_logo),
                               privacy_url=self.privacy_url,
                               released_claims=released_attributes,
                               form_action='/consent{}'.format(self.endpoint),
                               language=language)

        satosa_logging(logger, logging.INFO, "released attributes: {}".format(released_attributes), consent_state)
        return Response(page, content='text/html')

    def process(self, context, internal_response):
        """
        Ask the user for consent of data to be released.
        :param context: request context
        :param internal_response: the internal response
        """
        transaction_log(context.state, self.config.get("process_entry_order", 700),
                        "user_consent", "process", "entry", "success", '' , '', 'Requesting consent')

        consent_state = context.state[STATE_KEY]
        internal_response.attributes = {k: v for k, v in internal_response.attributes.items() if
                                        k in consent_state['filter']}
        consent_state['internal_response'] = internal_response.to_dict()
        handler = '/consent{}'.format(self.endpoint)
        return Redirect(handler)

    def accept_consent(self, context):
        """
        Endpoint for handling accepted consent.
        :type context: satosa.context.Context
        :rtype: satosa.response.Response

        :param context: response context
        :return: response
        """
        consent_state = context.state[STATE_KEY]
        saved_resp = consent_state['internal_response']
        internal_response = InternalResponse.from_dict(saved_resp)
        del context.state[STATE_KEY]

        log = {}
        log['router'] = context.state.state_dict['ROUTER']
        log['sessionid'] = context.state.state_dict['SESSION_ID']
        log['timestamp'] = saved_resp['auth_info'].get('timestamp')
        log['idp'] = saved_resp['auth_info'].get('issuer', None)
        log['rp'] = saved_resp.get('to', None)
        log['attr'] =saved_resp.get('attr', None)

        satosa_logging(logger, logging.INFO, "log: {}".format(log), context.state)
        print(json.dumps(log), file=self.loghandle, end="\n")
        self.loghandle.flush()

        transaction_log(context.state, self.config.get("consent_exit_order", 1000),
                        "user_consent", "accept", "exit", "success", '' , '', 'Consent given by the user')

        return super().process(context, internal_response)

    def deny_consent(self, context):
        """
        Endpoint for handling denied consent.
        :type context: satosa.context.Context
        :rtype: satosa.response.Response

        :param context: response context
        :return: response
        """
        del context.state[STATE_KEY]
        transaction_log(context.state, self.config.get("consent_exit_order", 1010),
                        "user_consent", "deny", "exit", "cancel", '', '', ErrorDescription.USER_CONSENT_DENIED[LOG_MSG],
                        'user')

        auth_error = SATOSAAuthenticationError(context.state, '')
        auth_error._message = ErrorDescription.USER_CONSENT_DENIED[ERROR_DESC]
        raise auth_error

    def handle_consent(self, context):
        consent_state = context.state[STATE_KEY]
        saved_resp = consent_state['internal_response']
        internal_response = InternalResponse.from_dict(saved_resp)

        lang = context.request.get('lang', 'en')
        return self.render_consent(consent_state, internal_response, lang)

    def register_endpoints(self):
        base = '^consent{}'.format(self.endpoint)
        url_map = []
        url_map.append(('{}$'.format(base), self.handle_consent))
        url_map.append(('{}/allow'.format(base), self.accept_consent))
        url_map.append(('{}/deny'.format(base), self.deny_consent))
        return url_map

    def _normalize_logo(self, requester_logo):
        if requester_logo:
            parsed_path = urlparse(requester_logo)
            if parsed_path.scheme in ['http', 'https']:
                normalized_path = requester_logo
            else:
                normalized_path = os.path.join(self.logo_base_path, requester_logo)
            return normalized_path
        else:
            return None
