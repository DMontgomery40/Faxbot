import React, { useEffect, useRef, useState } from 'react';
import {
  Box,
  Typography,
  Button,
  Alert,
  CircularProgress,
  Stack,
  Fade,
  Checkbox,
  FormControl,
  FormControlLabel,
  FormHelperText,
  Grow,
  InputLabel,
  ListItemText,
  MenuItem,
  Select,
  TextField,
  useTheme,
  useMediaQuery,
} from '@mui/material';
import { 
  Send as SendIcon,
  Phone as PhoneIcon,
  Description as DocumentIcon,
  CheckCircle as SuccessIcon,
  Error as ErrorIcon,
} from '@mui/icons-material';
import AdminAPIClient, {
  FaxRefusedError, FaxRetryMismatchError, isForbidden, normalizeFaxDestination, type FaxPatient,
} from '../api/client';
import type { AdminConfig, FaxEngineRequest, FaxSendResult } from '../api/types';
import {
  ResponsiveTextField,
  ResponsiveFileUpload,
  ResponsiveFormSection,
} from './common/ResponsiveFormFields';
import { clearPendingSend, loadPendingSend, savePendingSend, sendFingerprint } from './sendIntent';
import { countryName, numberHint, numberPlaceholder } from './common/numbers';
import type { BatchingCheck } from '../api/batchingTypes';
import type { RecipientCheck } from '../api/numberAdviceTypes';
import type { DocumentPrediction, RecommendedRoute, RoutePrediction } from '../api/deliveryTypes';
import { routeCostSentence } from './delivery/shared';
import { countPdfPages } from './common/pdfPages';
import ProviderRulesSendFields, { NO_SEND_OPTIONS, sendBody, type SendChoices, type SendOptions } from './ProviderRulesSendFields';
import { CoverInHeaderChoice } from './HeaderNotice';
import { CourtSendFields, courtJson } from './CourtProfile';
import type { CourtDetails } from '../api/courtTypes';
import { rulesApiFor } from './ProviderRulesApi';

interface SendFaxProps {
  client: AdminAPIClient;
  config: AdminConfig | null;
  configLoading: boolean;
  configError: string | null;
  // Open this fax in Sent (the confirmation links to it).
  onOpenJob?: (jobId: string) => void;
  // The mailboxes, workflows and labels rules can match (the console context's send block).
  sendChoices?: Partial<SendChoices> | null;
}

interface SubmissionIntent {
  key: string;
  destination: string;
  file: File;
  queueOnly: boolean;
  maxFileSizeBytes: number;
  fingerprint: string;
  createdAt: number;
}

function submissionKey(): string {
  if (typeof window.crypto?.randomUUID === 'function') return window.crypto.randomUUID();
  if (typeof window.crypto?.getRandomValues !== 'function') {
    throw new Error("This browser can't send faxes from the console. Nothing was sent.");
  }
  // getRandomValues remains available for ordinary HTTP self-hosted instances.
  const bytes = window.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function acceptanceMessage(response: FaxSendResult, to?: string): string {
  switch ((response.delivery_state || response.status).toLowerCase()) {
    case 'held':
      return to ? `Test fax for ${to} queued. It is held and will not be sent.` : 'Test fax queued. It is held and will not be sent.';
    case 'ready':
    case 'queued':
      return to ? `Fax queued for ${to}.` : 'Fax queued for sending.';
    case 'preparing':
      return 'Fax is being prepared.';
    case 'submitting':
    case 'in_progress':
      return 'Fax is sending.';
    case 'success':
    case 'completed':
      return 'Fax delivered.';
    case 'failed':
      return 'Fax failed. See Sent for details.';
    case 'cancelled':
    case 'canceled':
      return 'Fax cancelled.';
    case 'reconciliation_required':
      return "Faxbot couldn't confirm whether this fax was sent. Check Sent before sending it again.";
    default:
      return 'Fax submitted.';
  }
}

// The fax engine for one fax over your carrier trunk (POST /fax engine), in the API's order. The names are the ones
// Sent details says the choice in ("You chose the built-in engine for this fax."); each hint says what it does and,
// for the built-in engine, when to pick it.
export const ENGINE_CHOICES: ReadonlyArray<{ value: FaxEngineRequest; name: string; hint: string }> = [
  { value: 'auto', name: 'Automatic',
    hint: 'Faxbot chooses: the SSL Fax engine when it is running, otherwise the built-in engine.' },
  { value: 'builtin', name: 'Built-in engine',
    hint: 'Places an ordinary fax call. Pick it when faxes to this number keep failing on the SSL Fax engine.' },
  { value: 'hylafax', name: 'SSL Fax engine',
    hint: 'Pages go over SSL Fax when the other fax machine offers it. If this engine is not running, the built-in '
      + 'engine sends the fax.' },
];
const engineChoice = (value: FaxEngineRequest) => ENGINE_CHOICES.find((choice) => choice.value === value)
  ?? ENGINE_CHOICES[0];
// The main trunk's route key; more trunks are provider accounts of the trunk's kind ('sip').
const MAIN_TRUNK = 'sip';

const NO_PATIENT: FaxPatient = { recordNumber: '', recordSystem: '', familyName: '', givenName: '', birthDate: '' };

// The patient a fax is about, for a recipient whose health record system files documents under a patient (FHIR).
// Shown when such a system is among this number's routes, or on request for someone who may not see the routes;
// the details go with the fax and nowhere else.
function PatientFields({ routeLabel, value, onChange, disabled }: {
  routeLabel: string | null; value: FaxPatient; onChange: (value: FaxPatient) => void; disabled: boolean;
}) {
  const set = (key: keyof FaxPatient) => (event: React.ChangeEvent<HTMLInputElement>) =>
    onChange({ ...value, [key]: event.target.value });
  return (
    <Box data-testid="send-patient">
      <Typography variant="subtitle2">Patient (optional)</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        {routeLabel ? `${routeLabel} files this document in its health records.`
          : "Only for a recipient whose health record system takes your documents."}
        {' '}Give the patient so it lands in the right chart. If that system needs the patient and these are empty,
        Faxbot sends it as a fax instead.
      </Typography>
      <Box display="grid" gridTemplateColumns={{ xs: '1fr', sm: '1fr 1fr' }} gap={2}>
        <TextField size="small" label="Medical record number" value={value.recordNumber} onChange={set('recordNumber')}
          disabled={disabled} inputProps={{ maxLength: 64, autoComplete: 'off', 'data-testid': 'send-patient-number' }}
          helperText="The patient's number at the receiving organization." />
        <TextField size="small" label="Medical record number system (optional)" value={value.recordSystem} onChange={set('recordSystem')}
          disabled={disabled} inputProps={{ maxLength: 255, autoComplete: 'off', 'data-testid': 'send-patient-system' }}
          helperText="Leave it empty to use the one set on the FHIR client." />
        <TextField size="small" label="Family name" value={value.familyName} onChange={set('familyName')}
          disabled={disabled} inputProps={{ maxLength: 100, autoComplete: 'off', 'data-testid': 'send-patient-family' }} />
        <TextField size="small" label="Given name" value={value.givenName} onChange={set('givenName')}
          disabled={disabled} inputProps={{ maxLength: 100, autoComplete: 'off', 'data-testid': 'send-patient-given' }} />
        <TextField size="small" type="date" label="Birth date" value={value.birthDate} onChange={set('birthDate')}
          disabled={disabled} InputLabelProps={{ shrink: true }}
          inputProps={{ autoComplete: 'off', 'data-testid': 'send-patient-birth-date' }} />
      </Box>
    </Box>
  );
}

function SendFax({ client, config, configLoading, configError, onOpenJob, sendChoices }: SendFaxProps) {
  const theme = useTheme();
  const isSmallMobile = useMediaQuery(theme.breakpoints.down('sm'));
  
  const [toNumber, setToNumber] = useState('');
  // Who the fax is for (optional): before a first fax, NPPES may list the number for someone else.
  const [recipientName, setRecipientName] = useState('');
  // The name is checked once the sender leaves the field, never on each pause while typing.
  const [checkedName, setCheckedName] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [uploadPickerVersion, setUploadPickerVersion] = useState(0);
  const [loading, setLoading] = useState(false);
  // sentLink: the answer was lost or unconfirmed, so the fax may already be in Sent; the form keeps the number and
  // document, so sending again is one click and reuses the same send (never a second fax).
  const [result, setResult] = useState<{ type: 'success' | 'error' | 'warning' | 'info'; message: string; jobId?: string;
    sentLink?: boolean } | null>(null);
  const intentRef = useRef<SubmissionIntent | null>(null);
  const [resuming, setResuming] = useState(false);
  const submittingRef = useRef(false);

  // A number that sends faxes together offers "Send now" (go at once, taking the faxes waiting for it).
  const [together, setTogether] = useState<BatchingCheck | null>(null);
  const [sendNow, setSendNow] = useState(false);
  // One of this installation's own numbers: delivered inside Faxbot unless the sender asks for a real call.
  const [byCall, setByCall] = useState(false);
  // Urgent: before other faxes waiting for the same line, and never held to go with others.
  const [urgent, setUrgent] = useState(false);
  // The first page is a cover sheet whose notice is printed at the top of every page instead (header_notice.py).
  const [coverInHeader, setCoverInHeader] = useState(false);
  // A court fax (California court profile): the case details, or null for an ordinary fax.
  const [court, setCourt] = useState<CourtDetails | null>(null);
  const [sendOptions, setSendOptions] = useState<SendOptions>(NO_SEND_OPTIONS);
  // Optional: the time it must be sent by, in this browser's local time (a datetime-local value).
  const [sendBy, setSendBy] = useState('');
  // The number the batching answer is for: while it is on its way, Send now's row is held open, so the answer
  // never pushes Send down.
  const [togetherFor, setTogetherFor] = useState<string | null>(null);
  useEffect(() => {
    setTogether(null);
    setSendNow(false);
    if (!/\d{3}/.test(toNumber)) return undefined;
    let live = true;
    const timer = window.setTimeout(() => {
      client.checkBatching(toNumber)
        .then((answer) => { if (live) { setTogether(answer.sends_together ? answer : null); setTogetherFor(toNumber); } })
        .catch(() => { if (live) setTogetherFor(toNumber); });
    }, 400);
    return () => { live = false; window.clearTimeout(timer); };
  }, [client, toNumber]);
  const togetherPending = /\d{3}/.test(toNumber) && togetherFor !== toNumber;

  // Before a first fax to a number: a warning when NPPES lists it for another provider. It never stops the fax.
  const [recipientCheck, setRecipientCheck] = useState<RecipientCheck | null>(null);
  useEffect(() => {
    setRecipientCheck(null);
    if (!/\d{3}/.test(toNumber)) return undefined;
    let live = true;
    const timer = window.setTimeout(() => {
      client.recipientCheck(toNumber, checkedName || undefined)
        .then((answer) => { if (live) setRecipientCheck(answer); })
        .catch(() => undefined);
    }, 600);
    return () => { live = false; window.clearTimeout(timer); };
  }, [client, toNumber, checkedName]);

  // How many pages the chosen PDF has, so the estimate is for this document.
  const [pages, setPages] = useState<number | null>(null);
  useEffect(() => {
    setPages(null);
    if (!file) return undefined;
    let live = true;
    void countPdfPages(file).then((count) => { if (live) setPages(count); });
    return () => { live = false; };
  }, [file]);

  // The route Faxbot would use for this number and what this fax costs there, before sending.
  // People who may not read routing settings see the form without it.
  const [routes, setRoutes] = useState<RecommendedRoute[]>([]);
  // Someone who may not read routes (a fax operator) cannot see whether the number has a health record system.
  const [routesHidden, setRoutesHidden] = useState(false);
  // Whether the number is one of your own (its first route is 'local'), the routes this fax would try in order
  // (recommended) and every route you send by (available), per number: "send by call" and the fax engine keep their
  // rows while the first lookup runs, and asking again for the page count never hides them.
  const [localFor, setLocalFor] = useState<{ number: string; local: boolean; recommended: string[];
    available: string[] } | null>(null);
  useEffect(() => {
    setRoutes([]);
    if (!/\d{3}/.test(toNumber)) return undefined;
    let live = true;
    const asked = normalizeFaxDestination(toNumber);
    const timer = window.setTimeout(() => {
      client.getDestination(asked, pages ?? undefined)
        .then((detail) => {
          if (!live) return;
          setRoutes(detail.recommended_routes);
          setRoutesHidden(false);
          setLocalFor({ number: asked, local: detail.recommended_routes[0]?.route === 'local',
            recommended: detail.recommended_routes.map((item) => item.route),
            available: (detail.available_routes ?? []).map((item) => item.route) });
        })
        .catch((failure) => {
          if (!live) return;
          if (isForbidden(failure)) setRoutesHidden(true);
          setLocalFor({ number: asked, local: false, recommended: [], available: [] });
        });
    }, 400);
    return () => { live = false; window.clearTimeout(timer); };
  }, [client, toNumber, pages]);
  const route = routes[0] ?? null;
  const numberKey = /\d{3}/.test(toNumber) ? normalizeFaxDestination(toNumber) : null;
  const localPending = numberKey !== null && localFor?.number !== numberKey;
  const ownNumber = numberKey !== null && localFor?.number === numberKey && localFor.local;

  // Your carrier trunks by route key: the main trunk, and any more trunk accounts (someone who may not see accounts
  // still gets the main trunk). Read once; null until then.
  const [trunkKeys, setTrunkKeys] = useState<ReadonlySet<string> | null>(null);
  useEffect(() => {
    let live = true;
    rulesApiFor(client).accounts()
      .then((state) => {
        if (live) {
          setTrunkKeys(new Set([MAIN_TRUNK, ...state.accounts.filter((account) => account.provider === MAIN_TRUNK)
            .map((account) => account.key)]));
        }
      })
      .catch(() => { if (live) setTrunkKeys(new Set([MAIN_TRUNK])); });
    return () => { live = false; };
  }, [client]);
  // The fax engine for this one fax: it matters only for a fax over your phone line (a trunk), where one of Faxbot's
  // own engines places the call. Offered when a trunk is among the routes this fax would try, never only because a
  // trunk is set up (D27); one of your own numbers places no call unless Send by call is ticked. The number's routes
  // decide the row, so the late estimate never moves Send; the estimate only words it (engineLabel). Automatic unless
  // the sender picks one; posted only while it is offered.
  const [engine, setEngine] = useState<FaxEngineRequest>('auto');
  const enginePending = numberKey !== null && (localPending || trunkKeys === null);
  const engineOffered = !enginePending && numberKey !== null && localFor?.number === numberKey
    && (ownNumber ? byCall && [...localFor.recommended, ...localFor.available].some((key) => trunkKeys!.has(key))
      : localFor.recommended.some((key) => trunkKeys!.has(key)));
  const chosenEngine: FaxEngineRequest = engineOffered ? engine : 'auto';
  // A recipient's health record system (a FHIR route) among this number's routes asks for the patient; someone
  // who may not see the routes can add the patient on request.
  const recordsRoute = routes.find((item) => item.route.startsWith('fhir:')) ?? null;
  const [patient, setPatient] = useState<FaxPatient>(NO_PATIENT);
  const [patientAsked, setPatientAsked] = useState(false);
  const patientShown = recordsRoute !== null || (routesHidden && patientAsked);
  const givenPatient = patientShown && Object.values(patient).some((value) => (value ?? '').trim()) ? patient : undefined;

  // What would this cost? Once the document's pages are known, the shared predictor prices this fax on
  // that route; until then (or if it can't answer) the route's price in its own unit.
  const [prediction, setPrediction] = useState<RoutePrediction | null>(null);
  const routeKey = route?.route ?? null;
  useEffect(() => {
    setPrediction(null);
    if (!routeKey || !pages || !/\d{3}/.test(toNumber)) return undefined;
    let live = true;
    const timer = window.setTimeout(() => {
      client.predictCost(toNumber, pages)
        .then((answer) => { if (live) setPrediction(answer.routes.find((item) => item.route === routeKey) ?? null); })
        .catch(() => undefined);
    }, 400);
    return () => { live = false; window.clearTimeout(timer); };
  }, [client, toNumber, routeKey, pages]);

  // Validation states
  const [toNumberError, setToNumberError] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);

  // Better still: the document itself, its pages drawn and each fax coding measured on them (POST /routing/predict).
  // Asked once the number and the file have settled; any refusal or failure, or a route the answer does not cover
  // (local, partner, relay or digital routes), keeps the page-count price above.
  const [documentAnswer, setDocumentAnswer] = useState<DocumentPrediction | null>(null);
  const documentNumber = /\d/.test(toNumber) ? normalizeFaxDestination(toNumber) : null;
  useEffect(() => {
    setDocumentAnswer(null);
    if (!file || fileError || !documentNumber) return undefined;
    let live = true;
    const timer = window.setTimeout(() => {
      // Measured as the chosen engine would send the pages (Automatic: as Faxbot would choose).
      client.predictDocument(documentNumber, file, chosenEngine)
        .then((answer) => { if (live) setDocumentAnswer(answer); })
        .catch(() => undefined);
    }, 600);
    return () => { live = false; window.clearTimeout(timer); };
  }, [client, documentNumber, file, fileError, chosenEngine]);
  // With the document measured, the server names the account and pages the delivery worker would choose (each
  // account priced on its own best pages); the page-count recommendation above is then only a first guess and is
  // never shown with the measured account's price.
  const chosen = documentAnswer?.selected ?? null;
  const documentRoute = (chosen
    ? documentAnswer?.routes.find((item) => item.route === chosen.route)
    : documentAnswer?.routes.find((item) => item.route === routeKey)) ?? null;
  const shown: RoutePrediction | null = documentRoute ?? (chosen ? null : prediction);
  // A monthly plan carries this fax at no extra cost (the measured choice is in the plan, or the plan's estimate is
  // nothing extra). Its estimate opens with the plan and the cost ("Included in your HumbleFax plan ($10 a month):
  // this fax adds nothing to the bill") and then says the plan's normal-use budget, so nothing else in the panel
  // says the cost again (D23).
  const planCarries = Boolean(shown?.marginal) && (chosen ? chosen.in_plan && chosen.route === shown!.route
    : shown?.cost != null && Number(shown.cost.amount) === 0);
  const costSentence = shown ? (planCarries ? null : `What would this cost? ${shown.headline}`)
    : (route && !chosen ? routeCostSentence(route, null) : null);
  // A route first because faxes are included in its plan, or a plan past its normal-use budget first because no other
  // route can take the fax: its reason only repeats the plan's estimate, which says both.
  const planInBasis = (route?.reason === 'included' || route?.reason === 'over_budget') && planCarries;
  // The measured choice names the account, its pages and their price. For a plan's fax on its pages as given, with no
  // priced runner-up to compare and no hold, the route line and the estimate already say all of it.
  const choiceRepeats = chosen !== null && planCarries && chosen.layout === 'normal'
    && !documentAnswer?.runner_up?.cost && !documentAnswer?.held;
  // The route the estimate says this fax goes by: the measured choice, else the number's first route. Not one of your
  // trunks (and not a real call to one of your own numbers): the engine choice is worded as what happens if the fax
  // goes over your phone line after all, through a trunk later in the order.
  const firstRoute = chosen ?? route;
  const engineFallback = engineOffered && !(ownNumber && byCall) && firstRoute !== null
    && !trunkKeys!.has(firstRoute.route);
  const engineLabel = engineFallback ? 'Fax engine, if it goes over your phone line' : 'Fax engine';
  // The coding comparison is for Sent's details ("Fax coding"); the Send page gives the route, cost and time.
  const configReady = !configLoading && !configError && typeof config?.fax_disabled === 'boolean'
    && Number.isSafeInteger(config.max_file_size_mb) && config.max_file_size_mb > 0;
  const faxDisabled = configReady && config?.fax_disabled === true;
  const maxFileSizeMb = configReady ? config!.max_file_size_mb : null;
  const maxFileSizeBytes = maxFileSizeMb === null ? null : maxFileSizeMb * 1024 * 1024;
  const numberFormat = config?.number_format ?? null;
  const country = numberFormat ? countryName(numberFormat.country) : null;
  const localExample = numberFormat?.national.trim();

  // The server reads the number for the installation country and says when it
  // cannot; here a number only needs a digit.
  const validatePhone = (number: string): boolean => /\d/.test(number);

  const handleSend = async () => {
    if (submittingRef.current || !configReady) return;
    const destination = normalizeFaxDestination(toNumber);
    const retained = intentRef.current;
    const sameIntent = retained !== null && retained.destination === destination && retained.file === file;
    const allowedBytes = sameIntent ? retained.maxFileSizeBytes : maxFileSizeBytes;
    // Reset errors
    setToNumberError(false);
    setFileError(null);
    
    // Validate inputs
    let hasError = false;
    
    if (!toNumber.trim() || !validatePhone(toNumber)) {
      setToNumberError(true);
      hasError = true;
    }
    
    if (!file) {
      setFileError('Please select a PDF or TXT file.');
      hasError = true;
    } else if (allowedBytes !== null && file.size > allowedBytes) {
      setFileError(`Files can be up to ${allowedBytes / (1024 * 1024)} MB. Choose a smaller document.`);
      hasError = true;
    }
    
    if (hasError) {
      setResult({
        type: 'error',
        message: 'Please fix the errors above before submitting.',
      });
      return;
    }

    submittingRef.current = true;
    setLoading(true);
    setResult(null);

    try {
      // The engine this send asks for: the pick while it is offered, or the one the earlier send asked for.
      let postedEngine = engineOffered && engine !== 'auto' ? engine : undefined;
      if (!intentRef.current || intentRef.current.destination !== destination || intentRef.current.file !== file) {
        // The same document to the same number after a reload continues that send, with the engine it asked for.
        const fingerprint = sendFingerprint(destination, file!);
        const earlier = loadPendingSend(fingerprint);
        if (earlier) {
          postedEngine = earlier.engine;
          setEngine(earlier.engine ?? 'auto');
        }
        intentRef.current = earlier
          ? { key: earlier.key, destination, file: file!, queueOnly: earlier.queueOnly,
            maxFileSizeBytes: earlier.maxFileSizeBytes, fingerprint, createdAt: earlier.createdAt }
          : { key: submissionKey(), destination, file: file!, queueOnly: faxDisabled,
            maxFileSizeBytes: maxFileSizeBytes!, fingerprint, createdAt: Date.now() };
        setResuming(earlier !== null);
      }
      const intent = intentRef.current;
      // Kept before the upload starts, so a lost answer can be retried with the same key.
      savePendingSend({ key: intent.key, fingerprint: intent.fingerprint, queueOnly: intent.queueOnly,
        maxFileSizeBytes: intent.maxFileSizeBytes, createdAt: intent.createdAt, engine: postedEngine });
      const response = await client.sendFax(intent.destination, intent.file,
        { queueOnly: intent.queueOnly, idempotencyKey: intent.key, sendNow: together !== null && sendNow,
          byCall: ownNumber && byCall, urgent, coverInHeader, ...sendBody(sendOptions),
          sendBy: sendBy ? new Date(sendBy).toISOString() : undefined, patient: givenPatient, engine: postedEngine,
          court: court ? courtJson(court) : undefined, recipientName: recipientName.trim() || undefined });
      const state = (response.delivery_state || response.status).toLowerCase();
      const to = typeof response.to === 'string' && response.to ? response.to : undefined;
      setResult({
        type: state === 'reconciliation_required' ? 'warning' : state === 'failed' ? 'error'
          : state === 'success' || state === 'completed' ? 'success' : 'info',
        message: acceptanceMessage(response, to),
        jobId: response.id,
      });
      
      // Clear form on success; a new send gets a new key, even for the same document and number.
      intentRef.current = null;
      clearPendingSend();
      setResuming(false);
      setToNumber('');
      // The name belongs to the fax just sent, never to the next one.
      setRecipientName('');
      setCheckedName('');
      setFile(null);
      setPatient(NO_PATIENT);
      setPatientAsked(false);
      setEngine('auto');
      setUploadPickerVersion(version => version + 1);
      
    } catch (err) {
      if (err instanceof FaxRefusedError) {
        // Refused before acceptance: nothing was sent, so there is no send to resume.
        intentRef.current = null;
        clearPendingSend();
        setResuming(false);
        setResult({ type: 'error', message: err.message });
        return;
      }
      setResult({
        type: 'error',
        message: `${err instanceof TypeError ? "Couldn't reach the server, so the fax may or may not have been submitted."
          : err instanceof Error ? err.message : "Couldn't confirm the fax was submitted."}${intentRef.current
            && !(err instanceof FaxRetryMismatchError) ? ' To retry without creating a duplicate, send the same document to the same number again, even after reloading this page.' : ''}`,
        sentLink: true,
      });
    } finally {
      submittingRef.current = false;
      setLoading(false);
    }
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && e.target instanceof HTMLInputElement && e.target.type === 'tel'
        && !loading && configReady && toNumber && file) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <Box onKeyPress={handleKeyPress}>
      <Typography variant="h4" component="h1" gutterBottom sx={{ mb: 3 }}>
        {faxDisabled ? 'Queue a test fax' : 'Send a fax'}
      </Typography>

      {configLoading && <Alert severity="info" sx={{ mb: 3 }}>Loading send settings…</Alert>}
      {!configLoading && !configReady && <Alert severity="error" sx={{ mb: 3 }}>
        {configError ?? "Send settings couldn't be loaded. Reopen this page to try again."}
      </Alert>}
      {faxDisabled && <Alert severity="warning" sx={{ mb: 3 }}>
        Sending is turned off, so faxes queued here are held as tests and never sent.
      </Alert>}

      <Box sx={{ maxWidth: { xs: '100%', md: 800 } }}>
        <Fade in timeout={300}>
          <Box>
            <ResponsiveFormSection
              title={faxDisabled ? 'New test fax (sending is off)' : 'New fax'}
              subtitle={faxDisabled ? 'Queue a PDF or TXT document as a test' : 'Send a PDF or TXT document'}
              icon={<SendIcon />}
            >
              <Stack spacing={2}>
                <ResponsiveTextField
                  label="Destination Number"
                  value={toNumber}
                  onChange={(value) => {
                    if (submittingRef.current) return;
                    if (intentRef.current && intentRef.current.destination !== normalizeFaxDestination(value)) {
                      intentRef.current = null;
                    }
                    setToNumber(value);
                    if (toNumberError) setToNumberError(false);
                  }}
                  placeholder={numberPlaceholder(numberFormat)}
                  helperText={numberHint(numberFormat)}
                  type="tel"
                  disabled={!configReady || loading}
                  required
                  error={toNumberError}
                  errorMessage="Enter the fax number to send to."
                  icon={<PhoneIcon />}
                />

                <TextField size="small" label="Recipient name (optional)" value={recipientName}
                  onChange={(event) => setRecipientName(event.target.value)} disabled={!configReady || loading}
                  onBlur={() => setCheckedName(recipientName.trim())}
                  inputProps={{ maxLength: 200, 'data-testid': 'send-recipient-name' }}
                  helperText={'Shown with this fax wherever Faxbot lists it. Before a first fax, Faxbot checks the '
                    + 'number against the NPI registry (NPPES).'} />

                {!recordsRoute && routesHidden && !patientAsked && (
                  <Box>
                    <Button size="small" sx={{ px: 0, textTransform: 'none' }} onClick={() => setPatientAsked(true)}
                      disabled={!configReady || loading} data-testid="send-patient-add">
                      Add the patient's details
                    </Button>
                    <Typography variant="caption" color="text.secondary" display="block">
                      Only for a recipient whose health record system takes your documents.
                    </Typography>
                  </Box>
                )}
                {patientShown && (
                  <PatientFields routeLabel={recordsRoute?.label ?? null} value={patient} disabled={!configReady || loading}
                    onChange={(value) => { if (!submittingRef.current) setPatient(value); }} />
                )}

                <ResponsiveFileUpload
                  key={uploadPickerVersion}
                  label="Document to Fax"
                  value={file}
                  onFileSelect={(file) => {
                    if (submittingRef.current) return;
                    if (intentRef.current && intentRef.current.file !== file) intentRef.current = null;
                    setFile(file);
                    setFileError(file && maxFileSizeBytes !== null && file.size > maxFileSizeBytes
                      ? `Files can be up to ${maxFileSizeMb} MB. Choose a smaller document.`
                      : null);
                  }}
                  accept=".pdf,.txt,application/pdf,text/plain"
                  helperText={maxFileSizeMb === null ? 'PDF or TXT files only.' : `PDF or TXT, up to ${maxFileSizeMb} MB.`}
                  disabled={!configReady || loading}
                  required
                  error={!!fileError}
                  errorMessage={fileError ?? undefined}
                  icon={<DocumentIcon />}
                />

                <FormControlLabel data-testid="send-urgent"
                  control={<Checkbox checked={urgent} onChange={(e) => setUrgent(e.target.checked)}
                    disabled={!configReady || loading} />}
                  label="Urgent: send before other faxes waiting for the same line" />
                <CoverInHeaderChoice client={client} mailbox={sendOptions.mailbox} checked={coverInHeader}
                  onChange={setCoverInHeader} disabled={!configReady || loading || court !== null} />
                <CourtSendFields value={court} onChange={setCourt} disabled={!configReady || loading} />

                {sendChoices && (
                  <ProviderRulesSendFields value={sendOptions} onChange={setSendOptions} disabled={!configReady || loading}
                    choices={{ mailboxes: sendChoices.mailboxes ?? [], workflows: sendChoices.workflows ?? [],
                      labels: sendChoices.labels ?? [] }} />
                )}
                <TextField type="datetime-local" size="small" label="Send by (optional)" value={sendBy}
                  onChange={(e) => setSendBy(e.target.value)} disabled={!configReady || loading}
                  InputLabelProps={{ shrink: true }} inputProps={{ 'data-testid': 'send-by' }}
                  helperText="Faxbot sends it in time, even outside the recipient's usual hours."
                  sx={{ mt: 1, maxWidth: 320 }} />

                {/* Options that depend on a lookup keep their row (one checkbox high) while it runs, so the answer
                    never moves Send; informational answers go below Send instead. */}
                {localPending ? <Box aria-hidden data-testid="send-by-call-pending" sx={{ minHeight: 42 }} />
                  : ownNumber && (
                  <FormControlLabel data-testid="send-by-call"
                    control={<Checkbox checked={byCall} onChange={(e) => setByCall(e.target.checked)}
                      disabled={!configReady || loading} />}
                    label="Send it through your carrier with a real call instead, to test your fax line" />
                )}

                {togetherPending ? <Box aria-hidden data-testid="send-now-pending" sx={{ minHeight: 62 }} />
                  : together && (
                  <Box data-testid="send-now">
                    <Typography variant="body2" color="text.secondary">{together.sentence}</Typography>
                    <FormControlLabel
                      control={<Checkbox checked={sendNow} onChange={(e) => setSendNow(e.target.checked)}
                        disabled={!configReady || loading} />}
                      label="Send now (faxes already waiting for this number go in the same call)" />
                  </Box>
                )}

                {/* The fax engine: its row (a small select and its one-line hint) is held while the number's routes
                    are on their way. */}
                {enginePending ? <Box aria-hidden data-testid="send-engine-pending" sx={{ minHeight: 63 }} />
                  : engineOffered && (
                  <FormControl size="small" fullWidth disabled={!configReady || loading} data-testid="send-engine">
                    <InputLabel id="send-engine-label">{engineLabel}</InputLabel>
                    <Select labelId="send-engine-label" label={engineLabel} value={engine}
                      renderValue={(value) => engineChoice(value).name}
                      onChange={(event) => setEngine(event.target.value as FaxEngineRequest)}>
                      {ENGINE_CHOICES.map((choice) => (
                        <MenuItem key={choice.value} value={choice.value} sx={{ whiteSpace: 'normal' }}>
                          <ListItemText primary={choice.name} secondary={choice.hint} />
                        </MenuItem>
                      ))}
                    </Select>
                    <FormHelperText data-testid="send-engine-hint">
                      {engineFallback
                        ? `Faxbot plans to send it through ${firstRoute!.label}. This applies only if it goes over your `
                          + 'phone line instead.'
                        : engineChoice(engine).hint}
                    </FormHelperText>
                  </FormControl>
                )}

                <Box sx={{ 
                  display: 'flex', 
                  gap: 2, 
                  mt: 3,
                  flexDirection: { xs: 'column', sm: 'row' }
                }}>
                  <Button
                    variant="contained"
                    startIcon={loading ? <CircularProgress size={20} color="inherit" /> : <SendIcon />}
                    onClick={handleSend}
                    disabled={loading || !configReady}
                    size="large"
                    fullWidth={isSmallMobile}
                    sx={{
                      minWidth: { xs: '100%', sm: 200 },
                      height: { xs: 48, sm: 42 },
                      borderRadius: 2,
                      textTransform: 'none',
                      fontSize: '1rem',
                      fontWeight: 500,
                    }}
                  >
                    {loading ? 'Submitting…' : (faxDisabled ? 'Queue test fax' : 'Send Fax')}
                  </Button>

                  {(toNumber || file) && !loading && (
                    <Button
                      variant="outlined"
                      onClick={() => {
                        intentRef.current = null;
                        clearPendingSend();
                        setResuming(false);
                        setToNumber('');
                        setFile(null);
                        setPatient(NO_PATIENT);
                        setPatientAsked(false);
                        setEngine('auto');
                        setUploadPickerVersion(version => version + 1);
                        setResult(null);
                        setToNumberError(false);
                        setFileError(null);
                      }}
                      size="large"
                      fullWidth={isSmallMobile}
                      sx={{
                        minWidth: { xs: '100%', sm: 120 },
                        height: { xs: 48, sm: 42 },
                        borderRadius: 2,
                        textTransform: 'none',
                      }}
                    >
                      Clear
                    </Button>
                  )}
                </Box>

                {/* The recipient check, route and estimate arrive after the number and document (the document is
                    measured on the server), so they sit below Send and Clear: a late answer never moves a control
                    someone is about to click. */}
                {recipientCheck?.sentence && (
                  <Alert severity={recipientCheck.warning ? 'warning' : 'info'} data-testid="send-recipient-check">
                    {recipientCheck.sentence}
                  </Alert>
                )}
                {(route || chosen) && (
                  <Box data-testid="send-route">
                    <Typography variant="body2">Faxbot will send it through {chosen ? chosen.label : route!.label}.</Typography>
                    {chosen ? !choiceRepeats && (
                      <Typography variant="body2" color="text.secondary" data-testid="send-plan">
                        {documentAnswer!.sentence}
                      </Typography>
                    ) : !planInBasis && (
                      <Typography variant="body2" color="text.secondary" data-testid="send-route-reason">
                        {route!.explanation}
                      </Typography>
                    )}
                    {costSentence && (
                      <Typography variant="body2" color="text.secondary" data-testid="send-cost">{costSentence}</Typography>
                    )}
                    {shown && (
                      <Typography variant="caption" color="text.secondary" display="block" data-testid="send-cost-basis">
                        {shown.basis}
                      </Typography>
                    )}
                    {shown?.finish_sentence && (
                      <Typography variant="caption" color="text.secondary" display="block" data-testid="send-cost-finish">
                        {shown.finish_sentence}
                      </Typography>
                    )}
                    {!shown?.finish_sentence && shown?.time_note && (
                      <Typography variant="caption" color="text.secondary" display="block" data-testid="send-cost-time-note">
                        {shown.time_note}
                      </Typography>
                    )}
                  </Box>
                )}
              </Stack>
            </ResponsiveFormSection>
          </Box>
        </Fade>

        {resuming && !result && (
          <Alert severity="info" sx={{ mt: 3, borderRadius: 2 }}>Resuming your earlier send.</Alert>
        )}

        {/* Result Alert */}
        {result && (
          <Grow in timeout={300}>
            <Alert 
              severity={result.type}
              icon={result.type === 'success' ? <SuccessIcon /> : result.type === 'error' ? <ErrorIcon /> : undefined}
              sx={{ 
                mt: 3,
                borderRadius: 2,
                '& .MuiAlert-message': {
                  width: '100%'
                }
              }}
              onClose={() => setResult(null)}
            >
              <Box>
                <Typography variant="body1" fontWeight={500}>
                  {result.message}
                </Typography>
                {result.jobId && (onOpenJob ? (
                  <Button size="small" sx={{ mt: 1, px: 0 }} onClick={() => onOpenJob(result.jobId!)}>
                    See it in Sent
                  </Button>
                ) : (
                  <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>See it in Sent.</Typography>
                ))}
                {result.sentLink && !result.jobId && (
                  // Faxes → Sent's address (navigation.tsx: faxes/sent); the console follows the address.
                  <Button size="small" sx={{ mt: 1, px: 0 }} href="#/faxes/sent" data-testid="send-open-sent">
                    Open Faxes → Sent
                  </Button>
                )}
              </Box>
            </Alert>
          </Grow>
        )}

        {/* Instructions */}
        <Fade in timeout={600}>
          <Box sx={{ mt: 4 }}>
            <ResponsiveFormSection
              title="Quick Tips"
              subtitle="Prepare the document and follow it in Faxes → Sent"
            >
              <Stack spacing={2}>
                <Box>
                  <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 0.5 }}>
                    Fax Number Format
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    {country && localExample
                      ? <>• Numbers without a country code are read as {country} numbers, for example {localExample}<br /></>
                      : <>• Type local numbers the way you dial them<br /></>}
                    • For another country, start with + and its country code<br />
                    • Leave out extensions
                  </Typography>
                </Box>

                <Box>
                  <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 0.5 }}>
                    File Requirements
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    • PDF files: Standard documents, forms, letters<br />
                    • TXT files: Plain text will be converted to PDF automatically<br />
                    • Maximum file size: {maxFileSizeMb === null ? 'unavailable' : `${maxFileSizeMb} MB`}<br />
                    • Images: Convert to PDF first using a PDF creator
                  </Typography>
                </Box>

                <Box>
                  <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 0.5 }}>
                    Job Status
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    Follow each fax under Faxes, Sent; delivery time depends on your provider.
                  </Typography>
                </Box>
              </Stack>
            </ResponsiveFormSection>
          </Box>
        </Fade>
      </Box>
    </Box>
  );
}

export default SendFax;
