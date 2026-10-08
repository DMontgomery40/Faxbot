"""Every sentence connectors show the administrator or write to a sender, in one place.

One sentence per state. No protocol words on screens beyond the names an
administrator configures (DKIM, SPF, IMAP server); no times inside sentences
(screens format times themselves).
"""

# -- what became of one message or file (the items list) ------------------------------

FILED = 'Filed in {mailbox}.'
FILED_NUMBER = 'Filed for {number}.'
QUEUED = 'Sent on to be faxed to {number}.'
ACCEPTED_INTERNAL = 'Accepted: sent from inside your Microsoft 365 organization.'
DUPLICATE_RECEIVE = 'Seen again {count}; it was never filed twice.'
DUPLICATE_SEND = 'Seen again {count}; it was never faxed twice.'
CONFLICT = 'A later copy with the same identity held a different document; the first one is kept.'
CONFLICT_SEND = 'A later copy with the same identity asked for a different fax; nothing more was sent.'

NOT_AUTHENTICATED = ('Not sent: the message did not pass DKIM or SPF from your mail server, so its sender could '
                     'not be confirmed. No reply was sent.')
NO_TRUSTED_SERVER = ('Not sent: this connector does not name the mail server that checks senders, so no sender '
                     'can be confirmed. No reply was sent.')
NOT_LISTED = 'Not sent: {address} is not one of the people this connector may send faxes for.'
NO_SEND_PERMISSION = 'Not sent: {person} is not allowed to send faxes.'
NO_SEND_PASSWORD = ('Not sent: {person} has not replaced their temporary password yet, and until they do their '
                    'account cannot send faxes.')
NO_SENDER = 'Not sent: the message does not name exactly one sender. No reply was sent.'
NO_NUMBER = ('Not sent: the message gives no fax number. Put the number in the subject, such as +13035550100, '
             'or send to {example}.')
TWO_NUMBERS = 'Not sent: the message gives more than one fax number. Send one message for each fax number.'
BAD_NUMBER = 'Not sent: {text} is not a fax number Faxbot can dial.'
NO_DOCUMENT_SEND = 'Not sent: the message has no PDF or TIFF attachment.'
NO_DOCUMENT_RECEIVE = 'Not filed: the message has no PDF or TIFF attachment.'
UNREADABLE_SEND = 'Not sent: {name} is not a PDF or TIFF file Faxbot can read.'
UNREADABLE_RECEIVE = 'Not filed: {name} is not a PDF or TIFF file Faxbot can read.'
TOO_LARGE_SEND = 'Not sent: the message is larger than the {limit} MB limit.'
TOO_LARGE_RECEIVE = 'Not filed: the message is larger than the {limit} MB limit.'
OWN_MAIL = 'Not brought in: Faxbot sent this email itself, so it was skipped to avoid a loop.'
AUTOMATIC = 'Not brought in: the message was sent automatically, such as an out-of-office reply or a bounce.'
NO_MAILBOX = ('Not filed: this connector names no mailbox, and no sidecar file gave a fax number. Choose a '
              'mailbox for the connector.')
MAILBOX_GONE = 'Not filed: the mailbox this connector names no longer exists. Choose another mailbox.'
BAD_SIDECAR = 'Not done: the sidecar file could not be used. {detail}'
NO_SIDECAR_SEND = ('Not sent: no sidecar file with the fax number, such as {name}, arrived beside it within '
                   '{minutes} minutes.')
SENDING_OFF = 'Not sent: sending faxes is switched off in this installation.'
NO_SENDING_PROVIDER = 'Not sent: no fax provider is set up for sending.'
KEY_REVOKED = 'Waiting: the connector\'s own sending key was revoked. Resume the connector to give it a new key.'
SEND_FAILED = 'Not sent: Faxbot could not accept the fax. {detail}'
CONVERT_FAILED = 'Not sent: the document could not be prepared for faxing.'

# -- the connector itself (its status line) -----------------------------------------

NOT_CHECKED = 'Not checked yet.'
CHECKED_NOTHING = 'Nothing new.'
CHECKED = '{count} handled in the last check.'
PAUSED = 'Paused.'
PAUSED_BY = 'Paused by {person}.'
PAUSED_ITSELF = 'Paused by Faxbot: {reason}'
CANNOT_REACH = 'Faxbot could not reach the mail server {host}.'
SIGN_IN_REFUSED = 'The mail server did not accept the user name and password.'
TOKEN_REFUSED = 'The mail server did not accept the sign-in token.'
TOKEN_UNAVAILABLE = 'Faxbot could not get a sign-in token: {detail}'
NO_FOLDER = 'The mailbox has no folder named {folder}.'
MOVE_FAILED = 'The mail server would not move processed messages to {folder}; they will be checked again.'
MAIL_SERVER_ERROR = 'The mail server stopped answering during the check; Faxbot will try again.'
CHECK_STOPPED = 'The check stopped before it finished; Faxbot will try again.'
FOLDER_IS_FAXBOT = 'Choose another folder: {path} holds files Faxbot keeps for itself.'
NO_FINAL_RESULT = 'The fax had no final result after 7 days, so no result was sent.'
FOLDER_MISSING = 'Faxbot cannot read the folder {path}: it does not exist inside the Faxbot container.'
FOLDER_UNREADABLE = 'Faxbot cannot read or write the folder {path}.'
SECRET_UNREADABLE = 'The saved sign-in details could not be read; enter them again.'
TEST_OK_MAIL = 'Signed in to {host}; the folder {folder} holds {count}.'
TEST_OK_FOLDER = 'Faxbot can read and write {path}; it holds {count} waiting.'

# -- replies to an email sender -------------------------------------------------------

REPLY_SUBJECT_REFUSED = 'Fax not sent: {subject}'
REPLY_SUBJECT_SENT = 'Fax sent to {number}'
REPLY_SUBJECT_FAILED = 'Fax not sent to {number}'
REPLY_SUBJECT_UNCERTAIN = 'Fax to {number}: check before sending again'
REPLY_REFUSED = 'Faxbot did not send your fax. {reason} Nothing was sent.'
REPLY_SENT = 'Your fax to {number} was sent{pages}.'
REPLY_FAILED = 'Your fax to {number} was not sent. {detail} Nothing more will be tried; send it again when ready.'
REPLY_UNCERTAIN = ('Faxbot cannot tell whether your fax to {number} arrived. Ask the recipient before sending it '
                   'again, so they do not get it twice.')

# Plain reasons in a reply, rewritten for the sender (the administrator reads the item's own reason).
REPLY_NOT_LISTED = 'Your address is not allowed to send faxes through {address}.'
REPLY_NO_PERMISSION = 'Your Faxbot account is not allowed to send faxes.'
REPLY_NO_PASSWORD = 'Your Faxbot account can send faxes once you sign in to Faxbot and choose your own password.'


def times(count):
    return 'once' if count == 1 else ('twice' if count == 2 else f'{count} times')


def pages_phrase(pages):
    if not pages:
        return ''
    return ', 1 page' if pages == 1 else f', {pages} pages'
