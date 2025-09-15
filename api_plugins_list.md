Generate Faxbot Plugin Manifests for Outbound Fax APIs

Fax.Plus (Alohi Fax API)

Manifest:

{
  "id": "faxplus",
  "name": "Fax.Plus API",
  "auth": {
    "scheme": "bearer"
  },
  "actions": {
    "send_fax": {
      "method": "POST",
      "url": "https://restapi.fax.plus/v3/accounts/self/outbox",
      "headers": {
        "Content-Type": "application/json"
      },
      "body": {
        "kind": "json",
        "template": "{ \"to\": [ \"{{to}}\" ], \"files\": [ \"{{file_id}}\" ], \"send_time\": \"{{send_time}}\" }"
      },
      "response": {
        "faxId": "ids.{{to}}"
      }
    },
    "get_status": {
      "method": "GET",
      "url": "https://restapi.fax.plus/v3/accounts/self/outbox/{{fax_id}}",
      "headers": {},
      "body": {
        "kind": "none",
        "template": ""
      },
      "response": {
        "status": "status",
        "last_updated": "last_updated_status_time"
      }
    }
  },
  "allowed_domains": [
    "restapi.fax.plus"
  ],
  "timeout_ms": 15000
}


Overview: Fax.Plus (by Alohi) offers a robust RESTful fax API with OAuth2 support and personal access tokens (PATs) for authentication
fax.plus
fax.plus
. Developers can integrate outbound and inbound faxing into applications, with features like automatic file conversion and cloud storage integration
nordicapis.com
. To send a fax, you must first upload the document via the API (obtaining a file id), then call the SendFax endpoint with recipient number(s) and file reference
apidoc.fax.plus
apidoc.fax.plus
. The API returns a unique fax ID for tracking
apidoc.fax.plus
. A subsequent GET request provides status (e.g. “submitted”, “success”, or error) for the fax job
apidoc.fax.plus
apidoc.fax.plus
. Pricing: Fax.Plus has tiered plans. A Free plan includes 10 total pages; paid plans start at Basic (~$5.99–$7.99/month) with 200 pages/month included and $0.10 per extra page
fax.plus
fax.plus
. Higher tiers include Premium (500 pages, ~$13.99–$17.99/month, $0.07 extra)
fax.plus
fax.plus
, Business (1000 pages, ~$27.99–$34.99/month, $0.05 extra)
fax.plus
, and Enterprise (4000 pages, ~$79.99–$99.99/month, $0.03 extra)
fax.plus
fax.plus
. Enterprise plans include advanced features like Single Sign-On and a dedicated Fax.Plus API integration
fax.plus
fax.plus
.

RingCentral Fax API

Manifest:

{
  "id": "ringcentral",
  "name": "RingCentral Fax API",
  "auth": {
    "scheme": "bearer"
  },
  "actions": {
    "send_fax": {
      "method": "POST",
      "url": "https://platform.ringcentral.com/restapi/v1.0/account/~/extension/~/fax",
      "headers": {},
      "body": {
        "kind": "multipart",
        "template": "request={\"to\":[{\"phoneNumber\":\"{{to}}\"}]}&attachment={{file}}"
      },
      "response": {
        "faxId": "id"
      }
    },
    "get_status": {
      "method": "GET",
      "url": "https://platform.ringcentral.com/restapi/v1.0/account/~/message-store/{{fax_id}}",
      "headers": {},
      "body": {
        "kind": "none",
        "template": ""
      },
      "response": {
        "status": "messageStatus",
        "sentPages": "faxPageCount"
      }
    }
  },
  "allowed_domains": [
    "platform.ringcentral.com"
  ],
  "timeout_ms": 15000
}


Overview: RingCentral provides a cloud Fax API as part of its communications platform
nordicapis.com
nordicapis.com
. Authentication uses OAuth2 with Bearer tokens. To send a fax, the API uses a multipart HTTP request (RESTful endpoint POST /restapi/v1.0/account/~/extension/~/fax) where the request part contains JSON (recipient numbers, cover page text, etc.) and the attachment part contains the document (PDF/TIFF) to fax
medium.com
medium.com
. This allows sending multiple files and a custom cover page in one call
medium.com
medium.com
. The API returns a Fax Message ID used to track status. You can retrieve status via the message-store endpoint: status codes include Queued/Processing, Sent, or Error (with error details like busy, no answer, etc.)
faq.hellosign.com
faq.hellosign.com
. RingCentral’s API supports advanced features like retries on busy, customizable cover pages, and webhook or email confirmations
developers.ringcentral.com
developers.ringcentral.com
. Pricing: RingCentral offers standalone fax plans. As of recent data, Fax 3000 is about $27.99/month for up to 3,000 pages, and an Advanced (Unlimited fax) plan is ~$35/month (which also bundles voice/phone features)
ifaxapp.com
. Previously, a Fax 1500 plan (1500 pages) was ~$17.99/month (annual) or $22.99 monthly
bestreviews.net
, but current offerings emphasize the 3000-page and unlimited tiers
ifaxapp.com
. Overage fax pages typically cost around $0.01–$0.03 per page on RingCentral
ifaxapp.com
. (Note: Fax is included for RingCentral Office subscribers as well, with limits varying by plan.)

InterFAX (Upland InterFAX API)

Manifest:

{
  "id": "interfax",
  "name": "InterFAX API",
  "auth": {
    "scheme": "basic"
  },
  "actions": {
    "send_fax": {
      "method": "POST",
      "url": "https://rest.interfax.net/outbound/faxes?faxNumber={{to}}",
      "headers": {
        "Content-Location": "{{file_url}}",
        "Content-Type": "application/pdf"
      },
      "body": {
        "kind": "none",
        "template": ""
      },
      "response": {
        "faxId": "id"
      }
    },
    "get_status": {
      "method": "GET",
      "url": "https://rest.interfax.net/outbound/faxes/{{fax_id}}",
      "headers": {},
      "body": {
        "kind": "none",
        "template": ""
      },
      "response": {
        "status": "status"
      }
    }
  },
  "allowed_domains": [
    "rest.interfax.net"
  ]
}


Overview: InterFAX (an Upland Software service) provides a worldwide fax-sending API
nordicapis.com
nordicapis.com
. Authentication is via HTTP Basic (your InterFAX account ID and password)
docs.uplandsoftware.com
docs.uplandsoftware.com
. The REST API allows sending faxes by a single POST to the /outbound/faxes endpoint with the recipient fax number as a query parameter
docs.uplandsoftware.com
. The fax content can be supplied either by direct file upload (as the HTTP request body with appropriate Content-Type) or by reference using Content-Location header pointing to a document URL or previously uploaded file ID
docs.uplandsoftware.com
docs.uplandsoftware.com
. For example, you can provide a PDF URL via Content-Location and no body to have InterFAX fetch and fax that document
docs.uplandsoftware.com
. InterFAX supports many file types and will convert them automatically
docs.uplandsoftware.com
docs.uplandsoftware.com
. After submission, it returns a Fax ID, and you can poll GET /outbound/faxes/{id} for status updates (e.g., pending, sent, or error)
docs.uplandsoftware.com
. InterFAX also offers webhooks (callbacks) for fax status and completion reports
docs.uplandsoftware.com
. Pricing: InterFAX is a subscription-based service. Plans often start around $9.95/month for ~200 total pages (100 inbound + 100 outbound) with additional pages ~$0.09 each
ifaxapp.com
getapp.com
. Larger plans (e.g., 500 pages) cost around $22.95/month (250 in, 250 out)
uplandsoftware.com
getapp.com
. Enterprise and high-volume customers can negotiate custom plans; InterFAX also supports prepaid credits for usage
ifaxapp.com
. (InterFAX operates under eFax Corporate/Consensus for enterprise fax solutions as of 2025
efax.com
.)

Sfax (SecureFax by Consensus)

Manifest:

{
  "id": "sfax",
  "name": "Sfax API",
  "auth": {
    "scheme": "none"
  },
  "actions": {
    "send_fax": {
      "method": "POST",
      "url": "https://api.sfaxme.com/api/SendFax?token={{token}}&ApiKey={{api_key}}&RecipientName={{name}}&RecipientFax={{to}}",
      "headers": {
        "Content-Type": "application/pdf"
      },
      "body": {
        "kind": "binary",
        "template": "{{file_content}}"
      },
      "response": {
        "faxId": "Result"
      }
    },
    "get_status": {
      "method": "GET",
      "url": "https://api.sfaxme.com/api/ReceiveOutboundFaxStatus?token={{token}}&ApiKey={{api_key}}&FaxId={{fax_id}}",
      "headers": {},
      "body": {
        "kind": "none",
        "template": ""
      },
      "response": {
        "status": "FaxStatus"
      }
    }
  },
  "allowed_domains": [
    "api.sfaxme.com"
  ]
}


Overview: Sfax (by Consensus; formerly Scrypt) is a HIPAA-focused fax API designed for healthcare and other secure use cases
nordicapis.com
nordicapis.com
. The Sfax API uses a unique authentication scheme: each request includes an AES-encrypted token (computed from API Key, account username, and an encryption key/IV) and the API Key as parameters
sfax.scrypt.com
sfax.scrypt.com
. There is no persistent OAuth – a new token is generated for each call, valid ~15 minutes
sfax.scrypt.com
sfax.scrypt.com
. To send a fax, you call the SendFax endpoint with the recipient’s name and fax number, plus optional parameters for cover page and metadata
sfax.scrypt.com
sfax.scrypt.com
. The fax file content is sent in the HTTP request (as PDF/TIFF data) – Sfax expects a multipart form or binary POST with the document attached. (In practice, their SDKs handle constructing the request.) The API returns a Fax Job ID immediately, and you can query status with ReceiveOutboundFaxStatus or receive a callback when the fax completes. Security: Sfax enforces TLS encryption and offers a signed BAA for HIPAA compliance. Pricing: Sfax has flat-rate plans with included pages. The Standard plan is about $29/month for 350 pages, and Plus is ~$49/month for 700 pages
techradar.com
saasworthy.com
. Additional pages cost roughly $0.10 each, which is about industry average for secure fax
forbes.com
emitrr.com
. All plans include HIPAA compliance (secure storage, audit trails) and a BAA. Enterprise volumes and add-on API usage can be arranged via custom pricing.

Dropbox Fax API (HelloFax)

Manifest:

{
  "id": "dropbox_fax",
  "name": "Dropbox Fax API",
  "auth": {
    "scheme": "basic"
  },
  "actions": {
    "send_fax": {
      "method": "POST",
      "url": "https://api.hellofax.com/v1/Accounts/{{account_guid}}/Transmissions?To={{to}}",
      "headers": {},
      "body": {
        "kind": "multipart",
        "template": "file=@{{file_path}}"
      },
      "response": {
        "faxId": "Id"
      }
    },
    "get_status": {
      "method": "GET",
      "url": "https://api.hellofax.com/v1/Accounts/{{account_guid}}/Transmissions",
      "headers": {},
      "body": {
        "kind": "none",
        "template": ""
      },
      "response": {
        "status": "StatusCode",
        "error": "ErrorCode"
      }
    }
  },
  "allowed_domains": [
    "api.hellofax.com"
  ]
}


Overview: Dropbox Fax (formerly HelloFax) provides an API for sending and receiving faxes through Dropbox’s platform
faq.hellosign.com
. This API uses HTTP Basic Auth (your HelloFax/Dropbox Fax account email and password) for each request
faq.hellosign.com
. You must obtain your Account GUID (a unique identifier) from your account settings to use in the endpoint URLs
faq.hellosign.com
. To send a fax, you issue a POST /Accounts/{GUID}/Transmissions request with one or more To numbers as query params and attach the file content as multipart form data (-F file=@myfile.pdf)
faq.hellosign.com
. You can include multiple files and multiple recipients in one call by repeating those fields (the API supports array syntax)
faq.hellosign.com
. The API will return a Transmission record (with an ID and initial StatusCode). Fax statuses are represented by codes: P (Pending/processing), T (Transmitting), S (Success), E (Error), O (On hold/out of pages)
faq.hellosign.com
faq.hellosign.com
. You should set up callback URLs for real-time status updates: the API will POST to your configured URL when a fax succeeds or fails, including the final StatusCode and any ErrorCode (e.g., B=Busy, N=No answer, etc.)
faq.hellosign.com
faq.hellosign.com
. Pricing: The Dropbox Fax API is an enterprise-level add-on. It requires a monthly minimum of $100 usage
faq.hellosign.com
. Pricing is $0.05 per page (outbound or inbound) and $2.00 per fax line per month on this plan
faq.hellosign.com
. For example, $100 covers 2,000 pages in a month (additional usage beyond that will be billed at 5¢/page)
faq.hellosign.com
. This plan does not offer a free sandbox – you must have a paid API subscription to test and use it
faq.hellosign.com
. (Standard HelloFax non-API plans are cheaper but do not include API access; e.g., $9.99/mo for 300 pages on a regular plan without API.)

PamFax API

Manifest:

{
  "id": "pamfax",
  "name": "PamFax API",
  "auth": {
    "scheme": "none"
  },
  "actions": {
    "send_fax": {
      "method": "POST",
      "url": "https://api.pamfax.biz/FaxJob/Send",
      "headers": {},
      "body": {
        "kind": "form",
        "template": "user_token={{token}}&recipients[]=fax:{{to}}&file_ids[]={{file_id}}"
      },
      "response": {
        "faxId": "FaxJobId"
      }
    },
    "get_status": {
      "method": "GET",
      "url": "https://api.pamfax.biz/FaxJob/GetInfo?user_token={{token}}&faxjob_id={{fax_id}}",
      "headers": {},
      "body": {
        "kind": "none",
        "template": ""
      },
      "response": {
        "status": "status"
      }
    }
  },
  "allowed_domains": [
    "api.pamfax.biz"
  ]
}


Overview: PamFax is a cloud fax service that offers a free-to-use API for developers
pamfax.biz
pamfax.biz
. After signing up for a developer account, you receive an API key (apikey) and secret for generating request checksums, as well as a sandbox environment for testing
pamfax.biz
pamfax.biz
. The PamFax API uses a simple HTTPS POST interface with requests signed by an MD5 checksum (apicheck) of all parameters and your secret word for security
pamfax.biz
pamfax.biz
. You first call Session/VerifyUser with your credentials to obtain a UserToken
pamfax.biz
. This token is then used in subsequent API calls to act on behalf of that user. To send a fax, you would typically call FaxJob/Create (to start a fax job), FaxJob/UploadFile (or provide a file URL), then FaxJob/Send to transmit to specified recipient numbers
pamfax.biz
pamfax.biz
. The API can return XML or JSON based on an apioutputformat parameter
pamfax.biz
. PamFax provides rich features (cover pages, multiple recipients, tracking of costs) and returns detailed responses including success flags and FaxJob IDs. Pricing: The PamFax API itself is free to integrate (no monthly API fee)
pamfax.biz
. Usage of faxing requires credits or an active PamFax account. PamFax has a pay-as-you-go model: you purchase credit and each fax deducts a certain amount based on destination and pages. For example, rates might be around $0.10–$0.15 per page for many destinations. They also offer personal plans (e.g., €9.95 for 50 pages/month in some regions) and business packages; however, the API is often used in credit/prepaid mode where you either preload your account with credit or have end-users pay for their fax usage
pamfax.biz
pamfax.biz
. Notably, API developers are not charged extra beyond fax transmission costs – making PamFax’s API an attractive low-cost integration option.

GoFax (Australia) API

Overview: GoFax is an Australian fax service with an API suited for businesses in that region
nordicapis.com
. It offers fax sending and receiving with features like SMS notifications and is GITC government-accredited (meeting Australian government procurement standards)
nordicapis.com
. The API is secured and HIPAA-compliant, focusing on healthcare and enterprise customers. GoFax’s API likely uses a REST endpoint with basic authentication or API keys (their documentation is provided to customers upon request). Features include sending faxes to multiple recipients, querying fax status, and retrieving inbound faxes via polling or callbacks. Pricing: GoFax’s pricing is structured in AUD. They typically offer plans by page bundles. For example, inbound fax numbers might cost around AUD $9.95/month. Outbound usage is often pre-paid: e.g., pay-as-you-go at roughly $0.10 per page, with volume discounts for larger bundles. (Exact current pricing isn’t publicly cited here, but GoFax is positioned for enterprise, so custom quotes or plans might be available.) GoFax emphasizes compliance, providing BAAs and encryption for healthcare clients.

Notifyre Fax API

Overview: Notifyre is a secure online fax and SMS platform, with a strong emphasis on security for healthcare and enterprise. Its Fax API allows integration of fax send/receive into your applications
nordicapis.com
. Noteworthy features of Notifyre’s API include end-to-end encryption, audit trails, and support for fax number porting
nordicapis.com
. The API is HIPAA-compliant and requires API key authentication (likely a Bearer token in the Authorization header). Developers can send faxes by making HTTPS calls with JSON payloads specifying the recipient number and document (which can be uploaded or referenced). Notifyre also supports webhooks for status notifications, so you can receive real-time updates on fax delivery. Pricing: Notifyre typically offers a pay-per-use model. They have no monthly fee to have an account; you purchase fax credits that are deducted per page. For example, outbound faxes might cost about $0.07–$0.10 per page (in USD) depending on destination, and inbound fax numbers are available for a monthly fee (~$10/month in some regions). They also highlight free trial credits for developers to test the API. (Specific pricing varies by country and is available on contacting Notifyre.)

Concord Cloud Fax API

Overview: Concord Technologies provides enterprise cloud fax solutions. Their API (formerly known as FaxPress or FaxCOM for developers) allows high-volume fax submissions and tracking
concord.net
developer.concordfax.com
. Concord’s API supports both SOAP and REST interfaces: developers can choose JSON over HTTPS or SOAP/XML. To send a fax via the REST API, you authenticate using OAuth2 (Bearer token) or legacy credentials
developer.concordfax.com
developer.concordfax.com
. You then call the SendFax operation, providing recipient details and either attaching files or referencing files uploaded earlier
developer.concordfax.com
developer.concordfax.com
. Concord supports sending to multiple recipients and multiple files in one job
developer.concordfax.com
. The response returns an array of FaxJobIds (one per recipient) to track status
developer.concordfax.com
developer.concordfax.com
. Status retrieval is done via GET calls or using their notification callbacks. Concord’s API is very scalable, intended for high-volume transactional faxing (e.g., sending thousands of faxes for healthcare or financial documents). Pricing: Concord Fax is an enterprise service – pricing is typically custom. They often charge a monthly service fee plus a per-page fee. For example, a mid-size healthcare client might choose a plan for 1000 pages/month at a rate roughly around $0.05/page, with volume discounts as usage increases. There may be setup fees for dedicated cloud fax ports or integrations. Since Concord’s offerings are custom-tailored, they encourage contacting their sales for a quote
ifaxapp.com
. In general, Concord is chosen when reliability and integration flexibility are more important than low per-page cost.

SRFax API

Overview: SRFax is a long-running online fax provider known for its HIPAA compliance and flat-rate plans. The SRFax API is accessible via HTTP POST requests (it supports both REST/JSON and SOAP interfaces)
secure.srfax.com
. Authentication uses an API Key and secret which are passed in the request (either as JSON fields or form parameters). To send a fax, you call the unified endpoint with required fields: sFaxNumber (your account fax number), sFaxTarget (recipient number), and either a FileContent (base64 of the document) or a FileURL. SRFax can accept multiple documents and will queue the fax, returning a transaction ID. They also offer a status API to query the state of a fax by ID
secure.srfax.com
. SRFax’s documentation indicates that the same endpoint is used for all operations, with an action parameter indicating send, status, etc., making integration straightforward
secure.srfax.com
. Pricing: SRFax’s plans start around $10.95/month for 200 pages (incoming + outgoing combined). A popular plan is $17.95/month for 500 pages. Overage pages cost about $0.10 each. They also have higher plans (e.g., $38.95 for 2,500 pages). All plans include a fax number and unlimited storage of faxes for a year. The API access is included at no extra charge for all business plans. SRFax is known for not charging setup fees and allowing unused monthly pages to roll over for 30 days.

WestFax API

Overview: WestFax offers a RESTful API for secure faxing, often used in healthcare and enterprise scenarios. The API uses straightforward endpoints: e.g., POST https://apisecure.westfax.com/REST/Fax_SendFax/json to send a fax
westfax.com
. Authentication is done by including your WestFax account Username and Password in the form fields of each request (WestFax recommends creating a dedicated API user)
westfax.com
westfax.com
. In a send fax call, you provide parameters like ProductId (your fax line ID), Numbers (recipient fax number), and attach file content (Files0=@path/to/file.pdf) along with any cover page text or metadata
westfax.com
westfax.com
. The response returns a Job ID (GUID) for the fax
westfax.com
. WestFax supports advanced options such as scheduling faxes, setting the fax quality (Standard/Fine), and receiving email confirmations at a specified FeedbackEmail
westfax.com
westfax.com
. Pricing: WestFax has four monthly plans
ifaxapp.com
. Basic is $8.99/mo for 500 pages, Standard (Fax1500) is $29.95/mo for 1500 pages, Premium (Fax3000) is $59.95/mo for 3000 pages, and an Enterprise plan for higher volumes
ifaxapp.com
. Additional pages cost about $0.03 each on these plans
ifaxapp.com
. All plans include at least one fax number and secure storage. WestFax is HIPAA-compliant; the same pricing applies for their HIPAA plans (the Fax API is included for no extra fee in the Enterprise Fax API offering).

iFax API

Overview: iFax, known for its consumer and business fax apps, also provides a developer-friendly Fax API
ifaxapp.com
. The iFax API is REST-based with JSON, and it uses API keys (Bearer token auth) to authorize requests. Key features include the ability to send faxes, receive faxes (with webhook callbacks on new inbound faxes), and even bulk fax (broadcast) through a single API call. iFax highlights integrations with EHR/EMR systems and CRMs via its API
ifaxapp.com
. To send a fax, you POST a JSON including the recipient fax number(s) and either a PDF file (often provided as base64 string or a file URL). The API will queue the fax and return a transmission ID to query status. The status can also be delivered via webhook. Pricing: iFax offers scalable plans that include API access. The Enterprise API plan typically starts at $99/month, which comes with a large allocation of pages and multiple fax lines. Lower-tier business plans (e.g., $25 or $50/month) might include limited API access or none (the API is mainly targeted at enterprise level). On usage, iFax often charges around 5¢ per page beyond plan limits. For instance, an Enterprise plan might include 1,000 pages and then $0.05/page thereafter. (Exact API pricing isn’t publicly posted; it’s provided via consultation. However, iFax positions its API as a better-value alternative to others
ifaxapp.com
ifaxapp.com
, emphasizing that it offers robust features at competitive rates.)

eFax Corporate API

Overview: eFax (by J2/Consensus) offers an enterprise Fax API as part of eFax Corporate
efax.com
. The eFax Enterprise Fax API is RESTful and uses OAuth 2.0 for authentication (you obtain an access token via their identity service, then use Bearer tokens on fax calls)
efax.com
efax.com
. The API allows sending faxes by making a POST call with JSON payload including recipient number, cover page info, and either an attachment (often base64-encoded) or a pointer to a file in cloud storage
efax.com
. It supports high-volume fax jobs (many recipients & documents) and will handle retries, status callbacks, and audit logs automatically
efax.com
efax.com
. Inbound faxes can be retrieved via API or delivered to a webhook or email; the API provides endpoints to list and download received faxes. A notable feature of eFax’s API is its robust reporting: you can query transmission logs and get detailed metrics (success, failure reasons, timestamps, etc.) for compliance purposes
efax.com
efax.com
. Pricing: eFax Corporate’s API is available to enterprise accounts. They typically require a minimum monthly spend (often ~$100/month similar to Dropbox Fax API). Rates per page can be around $0.05 or lower with volume. For example, an enterprise might commit to 5,000 pages/month at $0.04 each. Additional fax numbers (DIDs) are usually around $10 each per month. Since eFax’s plans are custom, they offer volume-based discounts and contract terms. (SMB-oriented eFax plans like eFax Plus at $16.95/mo for 150 pages exist, but those do not include the API; API access is only with eFax Corporate service
ifaxapp.com
.)

Biscom Cloud Fax API

Overview: Biscom is an enterprise fax solution that offers multiple deployment models (Cloud Fax, On-Premise Fax server, Hybrid)
ifaxapp.com
. For developers, Biscom provides a REST API to integrate faxing into other software systems (e.g., EMR/EHR, CRM)
ifaxapp.com
. Using Biscom’s RESTful API, you can programmatically send faxes, check status, and retrieve received faxes. The API endpoints and authentication are not publicly documented (access is given to Biscom customers), but it’s known to support standard JSON over HTTPS and uses token-based auth or API keys. Typical usage involves sending a POST with recipient info and either uploading a document or referencing a file in the Biscom system, then polling an endpoint for fax status. Biscom’s platform can also integrate with physical fax machines/MFPs and VoIP, making it flexible for hybrid workflows
ifaxapp.com
ifaxapp.com
. Pricing: Biscom’s pricing is custom-quoted. They have plans often starting around $50/month for a few hundred pages and scaling up. One comparison mentioned a “Basic” Biscom cloud plan at $10 for 90 pages (likely promotional) and higher tiers like $50 for 500 pages, $100 for ~1100 pages, $250 for ~3100 pages
ifaxapp.com
. Biscom focuses on larger clients, so they commonly engage in annual contracts with committed page volumes. The API itself doesn’t cost extra as a feature, but it’s included as part of the enterprise service package
ifaxapp.com
. Biscom is often chosen when a business needs on-premise or hybrid deployment along with API integration, and its cost reflects an enterprise software model rather than a low-cost online service.

ClickSend Fax API

Overview: ClickSend is a multi-channel communications platform (SMS, Email, Fax). Its Fax API is a straightforward REST API that allows you to send faxes by making HTTPS calls to their endpoint. The typical workflow is: first use the /uploads endpoint to upload your document, which returns a file URL, then call /fax/send with the recipient number and that file URL
developers.clicksend.com
blog.clicksend.com
. The API uses HTTP Basic Auth (your ClickSend username and API key as the credentials) for all requests. Alternatively, you can use an API-Key header. A simple JSON POST to /fax/send might look like: { "to": "+15551234", "content": "https://rest.clicksend.com/.../yourfile.pdf" }. ClickSend’s API also supports sending fax cover page text and scheduling transmissions for later. Status of sent faxes can be retrieved via /fax/history endpoints or by callbacks (webhooks). Pricing: ClickSend operates on a pure usage model – no monthly fee, you pay per fax. Prices are approximately $0.07 to $0.10 per page (depending on destination country). For example, sending within the US might be ~$0.07/page. There’s a minimum deposit (e.g., $20 of credit) to start. Inbound fax numbers (optional) cost about $USD 6–$15/month (varies by country). Notably, ClickSend’s multi-channel platform means if you have an account, that credit can be used for any service (SMS, Fax, etc.).

SignalWire Fax API

Overview: SignalWire, a CPaaS platform from the original FreeSWITCH team, includes a Programmable Fax feature as part of its voice APIs
signalwire.com
. It’s designed as a Twilio-compatible API. You can send a fax by making a REST API call to SignalWire’s endpoint (similar to Twilio’s, e.g., POST /api/laml/2010-04-01/Accounts/{AccountID}/Faxes.json). JSON parameters include To, From (must be a SignalWire provided number that supports fax), and MediaUrl (link to a PDF/TIFF to fax). The API will then initiate the fax transmission and respond with a Fax SID (ID) and initial status. Status can be polled or you can use StatusCallback URLs to get asynchronous updates (e.g., delivered, failed). Pricing: SignalWire’s pricing is very usage-based. It charges per minute for fax transmissions (since it treats fax like a call). Typically it’s around $0.005 to $0.01 per minute for the call, plus perhaps a small per-fax fee. For a standard one-page fax that transmits in ~1 minute, the cost might be around half a cent to a cent. Inbound faxes to a SignalWire number are charged similarly per minute. SignalWire also has monthly number fees (~$1.50 per number). In practice, fax via SignalWire is extremely low-cost – often a few pennies per fax – making it a cost-effective API for those comfortable with a telephony-centric approach.

 

Sources:

Fax.Plus API documentation and pricing
apidoc.fax.plus
apidoc.fax.plus
fax.plus
fax.plus

RingCentral Fax API docs and pricing comparisons
medium.com
medium.com
ifaxapp.com

Upland InterFAX API docs and pricing info
docs.uplandsoftware.com
ifaxapp.com

Sfax API reference and third-party reviews
sfax.scrypt.com
techradar.com

Dropbox Fax (HelloFax) API help center
faq.hellosign.com
faq.hellosign.com

PamFax developer site
pamfax.biz
pamfax.biz

GoFax and Notifyre mentions in Nordic APIs
nordicapis.com

Concord Technologies API guide
developer.concordfax.com
developer.concordfax.com

SRFax API PDF and forum references
secure.srfax.com

WestFax API guide
westfax.com
westfax.com

iFax comparisons for pricing
ifaxapp.com

eFax Enterprise API blog
efax.com
 and HelloFax vs Biscom comparison
ifaxapp.com

ClickSend Fax API documentation
developers.clicksend.com

SignalWire Fax API reference (Twilio blog and SignalWire docs)
telnyx.com
signalwire.com

Sources