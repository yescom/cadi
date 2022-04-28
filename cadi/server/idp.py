import json
import re
import time

import cherrypy
from cadi.idp.ekyc import YES_CLAIMS, YES_VERIFIED_CLAIMS, ClaimsProvider
from cadi.idp.session import SessionManager
from cadi.rptestmechanics import RPTestResultStatus
from cadi.rptests.token import TokenRequestTestSet
from cadi.rptests.userinfo import UserinfoRequestTestSet
from cadi.server.userinterface import TEST_RESULT_STATUS_MAPPING
from furl import furl
from jwcrypto.jwt import JWT

from ..rptests.par import (
    PARRequestURIAuthorizationRequestTestSet,
    PushedAuthorizationRequestTestSet,
)
from ..rptests.traditional import TraditionalAuthorizationRequestTestSet
from ..tools import CLIENT_ID_PATTERN, json_handler, jwk_to_jwks


class IDP:
    MAX_TEST_RESULTS = 10
    TEST_RESULT_EXPIRATION = 60 * 60 * 12  # 12 hours
    MAX_RETRIES_CHECK_AND_SET = 12

    def __init__(
        self,
        platform_api,
        cache,
        j2env,
        server_jwk,
    ):
        self.platform_api = platform_api
        self.cache = cache
        self.j2env = j2env
        self.session_manager = SessionManager(cache)
        self.claims_provider = ClaimsProvider()
        self.server_jwk = server_jwk

    @cherrypy.expose
    @cherrypy.tools.json_out(handler=json_handler)
    def par(self, *args, **kwargs):
        client_id = self._test_for_client_id(kwargs)
        test = PushedAuthorizationRequestTestSet(
            self.platform_api,
            cache=self.cache,
            request=cherrypy.request,
            expected_client_id=client_id,
        )

        test_results = test.run()
        self._attach_test_results(client_id, test_results)

        if not "session" in test.data:
            cherrypy.response.status = 400
            return {
                "error": "server_error",
                "error_description": "We were not able to complete your request. "
                f"Please check the logs available at {cherrypy.request.base}/logs?client_id={client_id} for any errors.",
            }

        else:
            return {
                "request_uri": test.data["session"].request_uri,
                "expires_in": PARRequestURIAuthorizationRequestTestSet.REQUEST_URI_EXPIRE_WARNING_AFTER,
            }

    @cherrypy.expose
    def auth(self, *args, **kwargs):
        client_id = self._test_for_client_id(kwargs)

        if "request_uri" in kwargs:
            test_to_run = PARRequestURIAuthorizationRequestTestSet
        else:
            test_to_run = TraditionalAuthorizationRequestTestSet

        test = test_to_run(
            self.platform_api,
            cache=self.cache,
            request=cherrypy.request,
            expected_client_id=client_id,
        )

        test_results = test.run()
        self._attach_test_results(client_id, test_results)

        if "session" in test.data:
            session_id = test.data["session"].sid
            users_list = self.claims_provider.get_all_users()
        else:
            session_id = None
            users_list = []

        # Render the auth_ep.html template
        template = self.j2env.get_template("auth_ep.html")
        return template.render(
            client_id=client_id,
            test_results=test_results,
            stats=test_results.get_stats(),
            session_id=session_id,
            Status=RPTestResultStatus,
            SM=TEST_RESULT_STATUS_MAPPING,
            users_list=users_list,
        )

    @cherrypy.expose
    def auth_response_modifications(self, client_id, sid, user_id):
        session = self.session_manager.find(client_id=client_id, sid=sid)
        if session is None:
            raise cherrypy.HTTPError(
                400,
                "No session with this sid exists for this client ID. Please start a new authorization session.",
            )

        response_id_token_normal = json.dumps(
            self.claims_provider.process_ekyc_request(
                user_id, session, "id_token", False
            ),
            indent=2,
        )
        response_id_token_minimal = json.dumps(
            self.claims_provider.process_ekyc_request(
                user_id, session, "id_token", True
            ),
            indent=2,
        )
        response_userinfo_normal = json.dumps(
            self.claims_provider.process_ekyc_request(
                user_id, session, "userinfo", False
            ),
            indent=2,
        )
        response_userinfo_minimal = json.dumps(
            self.claims_provider.process_ekyc_request(
                user_id, session, "userinfo", True
            ),
            indent=2,
        )

        # Render the auth_ep.html template
        template = self.j2env.get_template("auth_ep_resp_mod.html")
        return template.render(
            client_id=client_id,
            session_id=session.sid,
            response_id_token_normal=response_id_token_normal,
            response_id_token_minimal=response_id_token_minimal,
            response_userinfo_normal=response_userinfo_normal,
            response_userinfo_minimal=response_userinfo_minimal,
        )

    @cherrypy.expose
    def auth_continue(
        self,
        client_id,
        sid,
        id_token_content_selector,
        id_token_content_left,
        id_token_content_right,
        userinfo_content_selector,
        userinfo_content_left,
        userinfo_content_right,
    ):
        session = self.session_manager.find(client_id=client_id, sid=sid)
        if session is None:
            raise cherrypy.HTTPError(
                400,
                "No session with this sid exists for this client ID. Please start a new authorization session.",
            )

        if id_token_content_selector == "left":
            session.id_token_response_contents = json.loads(id_token_content_left)
        else:
            session.id_token_response_contents = json.loads(id_token_content_right)

        if userinfo_content_selector == "left":
            session.userinfo_response_contents = json.loads(userinfo_content_left)
        else:
            session.userinfo_response_contents = json.loads(userinfo_content_right)

        # Use furl library to assemble the redirect URI with the redirect URI from the session.
        # Parameters: state, code, and iss
        redirect_uri = furl(session.redirect_uri)
        if session.state is not None:
            redirect_uri.args["state"] = session.state
        redirect_uri.args["code"] = session.authorization_code
        redirect_uri.args["iss"] = cherrypy.request.base + "/idp"

        # Redirect browser to redirect_uri
        raise cherrypy.HTTPRedirect(redirect_uri.url)

    @cherrypy.expose
    @cherrypy.tools.json_out(handler=json_handler)
    def token(self, *args, **kwargs):
        client_id = self._desperately_find_client_id(kwargs, cherrypy.request)
        if not client_id:
            raise cherrypy.HTTPError(
                400,
                "Missing client_id! We looked for it in the URL and the request body, but we couldn't find it.",
            )

        test = TokenRequestTestSet(
            self.platform_api,
            cache=self.cache,
            request=cherrypy.request,
            expected_client_id=client_id,
        )

        test_results = test.run()
        self._attach_test_results(client_id, test_results)

        if not "session" in test.data:
            cherrypy.response.status = 400
            return {
                "error": "server_error",
                "error_description": "We were unable to identify the session to which your request belongs. "
                "Please ensure that your token request is conformant to the token request format defined in RFC6749! "
                f"Please check the logs available at {cherrypy.request.base}/logs?client_id={client_id} for any errors.",
            }

        session = test.data["session"]

        id_token = self._create_id_token(session)

        return {
            "access_token": session.access_token,
            "id_token": id_token,
            "token_type": "Bearer",
            "expires_in": SessionManager.SESSION_EXPIRATION,
            "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
        }

    @cherrypy.expose
    @cherrypy.tools.json_out(handler=json_handler)
    def userinfo(self):
        authorization_header = cherrypy.request.headers.get("authorization", None)

        if authorization_header is None:
            return {
                "error": "invalid_request",
                "error_description": "The request does not contain an 'Authorization' header. "
                "You must provide the 'Authorization' header with an access token. "
                "Please review the OpenID Connect core specification for the userinfo endpoint.",
            }

        if not authorization_header.startswith("Bearer "):
            return {
                "error": "invalid_request",
                "error_description": "The Authorization header provided does not start with 'Bearer '. "
                "Please review the OpenID Connect core specification for the userinfo endpoint.",
            }

        session = self.session_manager.find_by_access_token(authorization_header[7:])

        if session is None:
            return {
                "error": "invalid_request",
                "error_description": "The access token provided in the Authorization header is unknown or has expired. "
                "Please review the OpenID Connect core specification for the userinfo endpoint.",
            }

        client_id = session.client_id

        test = UserinfoRequestTestSet(
            platform_api=self.platform_api,
            cache=self.cache,
            client_id=client_id,
            request=cherrypy.request,
            session=session,
        )

        test_results = test.run()
        self._attach_test_results(client_id, test_results)

        return session.userinfo_response_contents

    @cherrypy.expose
    @cherrypy.tools.json_out(handler=json_handler)
    def jwks(self):
        # JWKS Endpoint: Serve the server certificate
        return jwk_to_jwks(self.server_jwk)

    def _attach_test_results(self, client_id, test_results):
        key = ("test_results", client_id)
        self.cache.insert_into_list(
            key,
            test_results,
            self.MAX_TEST_RESULTS,
            self.TEST_RESULT_EXPIRATION,
        )

    def _desperately_find_client_id(self, parameters, request):
        # Check if the client ID is in the URL or the form-encoded body
        if "client_id" in parameters and re.match(
            CLIENT_ID_PATTERN, parameters["client_id"]
        ):
            return parameters["client_id"]

        # try to decode the body if it is json and extract the client_id
        try:
            body = request.body.read().decode("utf-8")
            try:
                payload = json.loads(body)
                if re.match(CLIENT_ID_PATTERN, payload["client_id"]):
                    return payload["client_id"]
            except Exception:
                pass

            # try to extract (using a regex) the client_id from the body and the query
            client_id = re.search(CLIENT_ID_PATTERN, body)
            if client_id:
                return client_id.group(0)
        except Exception:
            pass

        client_id = re.search(CLIENT_ID_PATTERN, request.request_line)
        if client_id:
            return client_id.group(0)

        return None

    def _test_for_client_id(self, kwargs):
        client_id = self._desperately_find_client_id(kwargs, cherrypy.request)
        if not client_id:
            raise cherrypy.HTTPError(
                400,
                "Missing client_id! We looked for it in the URL and the request body, but we couldn't find it.",
            )
        return client_id

    def _create_id_token(self, session):
        claims = session.id_token_response_contents
        claims['iss'] = cherrypy.request.base + "/idp"
        claims['aud'] = session.client_id
        claims['iat'] = int(time.time())
        claims['exp'] = int(time.time()) + 3600
        claims['nonce'] = session.nonce
        # TODO: test all these, including acr!

        # Create a signed ID token using the server's private key
        id_token = JWT(
            header={"kid": "default", "alg": "RS256"},
            claims=claims,
            key=self.server_jwk,
        )
        id_token.make_signed_token(self.server_jwk)
        return id_token.serialize()

    @staticmethod
    def json_error_page(status, message, traceback, version):
        cherrypy.response.headers["Content-Type"] = "application/json"
        return json.dumps({"error_description": f"Error while processing your message: '{message}'\n\n{traceback}", "error": "server_error"})
  

"""
Serve the OpenID Connect well-known file on the following web server URLs:
    /.well-known/openid-configuration
    /.well-known/oauth-configuration

The well-known file is a JSON document that describes the OpenID Connect and OAuth 2.0 endpoints. 
The contents are mostly a static configuration, but some paths depend on the web server domain, for example.
"""


class WellKnown:
    @cherrypy.expose(alias=["openid_configuration", "oauth_configuration"])
    @cherrypy.tools.json_out(handler=json_handler)
    def index(self):
        return {
            "issuer": f"{cherrypy.request.base}/idp",
            "authorization_endpoint": f"{cherrypy.request.base}/idp/auth?{TraditionalAuthorizationRequestTestSet.DUMMY_PARAMETER}=42",
            "token_endpoint": f"{cherrypy.request.base}/idp/token",
            "userinfo_endpoint": f"{cherrypy.request.base}/idp/userinfo",
            "pushed_authorization_request_endpoint": f"{cherrypy.request.base}/idp/push_auth",
            "jwks_uri": f"{cherrypy.request.base}/idp/jwks",
            "scopes_supported": ["openid"],
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code"],
            "acr_values_supported": [
                "https://www.yes.com/acrs/online_banking",
                "https://www.yes.com/acrs/online_banking_sca",
            ],
            "id_token_signing_alg_values_supported": ["RS256"],
            "userinfo_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["plain", "S256"],
            "token_endpoint_auth_methods_supported": ["self_signed_tls_client_auth"],
            "request_parameter_supported": True,
            "request_uri_parameter_supported": True,
            "require_request_uri_registration": True,
            "authorization_response_iss_parameter_supported": True,
            "tls_client_certificate_bound_access_tokens": True,
            "authorization_data_types_supported": ["payment_initiation", "sign"],
            "verification_methods_supported": [
                {
                    "identity_document": [
                        "Physical In-Person Proofing (bank)",
                        "Physical In-Person Proofing (shop)",
                        "Physical In-Person Proofing (courier)",
                        "Supervised remote In-Person Proofing",
                    ]
                },
                "qes",
                "eID",
            ],
            "claim_types_supported": ["normal"],
            "claims_supported": ["sub"] + list(YES_CLAIMS.keys()),
            "claims_parameter_supported": True,
            "verified_claims_supported": True,
            "trust_frameworks_supported": ["de_aml"],
            "evidence_supported": ["id_document"],
            "documents_supported": [
                "idcard",
                "passport",
                "de_idcard_foreigners",
                "de_emergency_idcard",
                "de_erp",
                "de_erp_replacement_idcard",
                "de_idcard_refugees",
                "de_idcard_apatrids",
                "de_certificate_of_suspension_of_deportation",
                "de_permission_to_reside",
                "de_replacement_idcard",
            ],
            "documents_methods_supported": ["pipp", "sripp"],
            "claims_in_verified_claims_supported": list(YES_VERIFIED_CLAIMS.keys()),
        }