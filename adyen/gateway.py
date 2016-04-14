import base64
import hashlib
import hmac
import logging

from .constants import Constants
from .exceptions import (
    InvalidTransactionException,
    MissingFieldException,
    MissingParameterException,
    UnexpectedFieldException,
)

logger = logging.getLogger('adyen')


# ---[ GATEWAY ]---

class Gateway:

    MANDATORY_SETTINGS = (
        Constants.IDENTIFIER,
        Constants.SECRET_KEY,
        Constants.ACTION_URL,
        Constants.SIGNER,
    )

    def __init__(self, settings=None):
        """
        Initialize an Adyen gateway.
        """
        if settings is None:
            settings = {}

        if any(key not in settings for key in self.MANDATORY_SETTINGS):
            raise MissingParameterException(
                "You need to specify the following parameters to initialize "
                "the Adyen gateway: %s. "
                "Please check your configuration."
                % ', '.join(self.MANDATORY_SETTINGS))

        self.identifier = settings.get(Constants.IDENTIFIER)
        self.secret_key = settings.get(Constants.SECRET_KEY)
        self.action_url = settings.get(Constants.ACTION_URL)
        self.signer = settings.get(Constants.SIGNER)

    def _compute_hash(self, keys, params):
        """
        Compute a validation hash for Adyen transactions.

        General method:

        The signature is computed using the HMAC algorithm with the SHA-1
        hashing function. The data passed, in the form fields, is concatenated
        into a string, referred to as the “signing string”. The HMAC signature
        is then computed over using a key that is specified in the Adyen Skin
        settings. The signature is passed along with the form data and once
        Adyen receives it, they use the key to verify that the data has not
        been tampered with in transit. The signing string should be packed
        into a binary format containing hex characters, and then base64-encoded
        for transmission.

        Payment Setup:

        When setting up a payment the signing string is as follows:

        paymentAmount + currencyCode + shipBeforeDate + merchantReference
        + skinCode + merchantAccount + sessionValidity + shopperEmail
        + shopperReference + recurringContract + allowedMethods
        + blockedMethods + shopperStatement + merchantReturnData
        + billingAddressType + deliveryAddressType + shopperType + offset

        The order of the fields must be exactly as described above.
        If you are not using one of the fields, such as allowedMethods,
        the value for this field in the signing string is an empty string.

        Payment Result:

        The payment result uses the following signature string:

        authResult + pspReference + merchantReference + skinCode
        + merchantReturnData
        """
        signature = ''.join(str(params.get(key, '')) for key in keys)
        hm = hmac.new(self.secret_key.encode(), signature.encode(), hashlib.sha1)
        hash_ = base64.encodebytes(hm.digest()).strip().decode('utf-8')
        return hash_

    def _build_form_fields(self, adyen_request):
        """
        Return the hidden fields of an HTML form allowing to perform this request.
        """
        return adyen_request.build_form_fields()

    def build_payment_form_fields(self, params):
        return self._build_form_fields(PaymentFormRequest(self, params))

    def _process_response(self, adyen_response, params):
        """
        Process an Adyen response.
        """
        return adyen_response.process()


class BaseInteraction:
    REQUIRED_FIELDS = ()
    OPTIONAL_FIELDS = ()

    def validate(self):
        self.check_fields()

    def check_fields(self):
        """
        Validate required and optional fields for both
        requests and responses.
        """
        params = self.params

        # Check that all mandatory fields are present.
        for field_name in self.REQUIRED_FIELDS:
            if not params.get(field_name):
                raise MissingFieldException(
                    "The %s field is missing" % field_name
                )

        # Check that no unexpected field is present.
        expected_fields = self.REQUIRED_FIELDS + self.OPTIONAL_FIELDS
        for field_name in params.keys():
            if field_name not in expected_fields:
                raise UnexpectedFieldException(
                    "The %s field is unexpected" % field_name
                )


# ---[ FORM-BASED REQUESTS ]---

class PaymentFormRequest(BaseInteraction):
    REQUIRED_FIELDS = (
        Constants.MERCHANT_ACCOUNT,
        Constants.MERCHANT_REFERENCE,
        Constants.SHOPPER_REFERENCE,
        Constants.SHOPPER_EMAIL,
        Constants.CURRENCY_CODE,
        Constants.PAYMENT_AMOUNT,
        Constants.SESSION_VALIDITY,
        Constants.SHIP_BEFORE_DATE,
    )
    OPTIONAL_FIELDS = (
        Constants.MERCHANT_SIG,
        Constants.SKIN_CODE,
        Constants.RECURRING_CONTRACT,
        Constants.ALLOWED_METHODS,
        Constants.BLOCKED_METHODS,
        Constants.SHOPPER_STATEMENT,
        Constants.SHOPPER_LOCALE,
        Constants.COUNTRY_CODE,
        Constants.MERCHANT_RETURN_URL,
        Constants.MERCHANT_RETURN_DATA,
        Constants.BILLING_ADDRESS_TYPE,
        Constants.DELIVERY_ADDRESS_TYPE,
        Constants.SHOPPER_TYPE,
        Constants.OFFSET,
    )

    def __init__(self, client, params=None):
        self.client = client
        self.params = params or {}
        self.validate()

        # Compute request hash.
        self.params.update(
            self.client.signer.sign(self.params))

    def build_form_fields(self):
        return [{'type': 'hidden', 'name': name, 'value': value}
                for name, value in self.params.items()]


# ---[ RESPONSES ]---

class BaseResponse(BaseInteraction):

    def __init__(self, client, params):
        self.client = client
        self.secret_key = client.secret_key
        self.params = params

    def process(self):
        return NotImplemented


class PaymentNotification(BaseResponse):
    """Process payment notifications (HTTPS POST from Adyen to our servers).

    Payment notifications can have multiple fields. They fall into four
    categories:

    - required: Must be included.
    - optional: Can be included.
    - additional data: Can be included. Format is 'additionalData.VALUE' and
      we don't need the data at the moment, so it's ignored.
    - unexpected: We loudly complain.

    """
    REQUIRED_FIELDS = (
        Constants.CURRENCY,
        Constants.EVENT_CODE,
        Constants.EVENT_DATE,
        Constants.LIVE,
        Constants.MERCHANT_ACCOUNT_CODE,
        Constants.MERCHANT_REFERENCE,
        Constants.PAYMENT_METHOD,
        Constants.PSP_REFERENCE,
        Constants.REASON,
        Constants.SUCCESS,
        Constants.VALUE,  # The payment amount may be retrieved here.
    )
    OPTIONAL_FIELDS = (
        Constants.OPERATIONS,
        Constants.ORIGINAL_REFERENCE,
    )

    def check_fields(self):
        """
        Delete unneeded additional data before validating.

        Adyen's payment notification can come with additional data.
        It can mostly be turned on and off in the notifications settings,
        but some bits always seem to be delivered with the new
        "System communication" setup (instead of the old "notifications" tab
        in the settings).
        We currently don't need any of that data, so we just drop it
        before validating the notification.
        :return:
        """
        self.params = {
            key: self.params[key]
            for key in self.params if Constants.ADDITIONAL_DATA_PREFIX not in key
        }
        super().check_fields()

    def process(self):
        payment_result = self.params.get(Constants.SUCCESS, None)
        accepted = payment_result == Constants.TRUE
        status = (Constants.PAYMENT_RESULT_AUTHORISED if accepted
                  else Constants.PAYMENT_RESULT_REFUSED)
        return accepted, status, self.params


class PaymentRedirection(BaseResponse):
    """Process payment feedback from the user

    When they paid on Adyen and get redirected back to our site. HTTP GET from
    user's browser.
    """
    REQUIRED_FIELDS = (
        Constants.AUTH_RESULT,
        Constants.MERCHANT_REFERENCE,
        Constants.MERCHANT_SIG,
        Constants.SHOPPER_LOCALE,
        Constants.SKIN_CODE,
    )
    OPTIONAL_FIELDS = (
        Constants.MERCHANT_RETURN_DATA,  # The payment amount may be retrieved here.
        Constants.PAYMENT_METHOD,
        Constants.PSP_REFERENCE,
    )

    def validate(self):
        super().validate()
        # Check that the transaction has not been tampered with.
        if not self.client.signer.verify(self.params):
            raise InvalidTransactionException(
                "The transaction is invalid. This may indicate a fraud attempt.")

    def process(self):
        payment_result = self.params[Constants.AUTH_RESULT]
        accepted = payment_result == Constants.PAYMENT_RESULT_AUTHORISED
        return accepted, payment_result, self.params
