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
  FormControlLabel,
  Grow,
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
import AdminAPIClient, { FaxRefusedError, isForbidden, normalizeFaxDestination, type FaxPatient } from '../api/client';
import type { AdminConfig, FaxSendResult } from '../api/types';
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
  const [result, setResult] = useState<{ type: 'success' | 'error' | 'warning' | 'info'; message: string; jobId?: string } | null>(null);
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
  const [sendOptions, setSendOptions] = useState<SendOptions>(NO_SEND_OPTIONS);
  // Optional: the time it must be sent by, in this browser's local time (a datetime-local value).
  const [sendBy, setSendBy] = useState('');
  useEffect(() => {
    setTogether(null);
    setSendNow(false);
    if (!/\d{3}/.test(toNumber)) return undefined;
    let live = true;
    const timer = window.setTimeout(() => {
      client.checkBatching(toNumber)
        .then((answer) => { if (live) setTogether(answer.sends_together ? answer : null); })
        .catch(() => undefined);
    }, 400);
    return () => { live = false; window.clearTimeout(timer); };
  }, [client, toNumber]);

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
  useEffect(() => {
    setRoutes([]);
    if (!/\d{3}/.test(toNumber)) return undefined;
    let live = true;
    const timer = window.setTimeout(() => {
      client.getDestination(normalizeFaxDestination(toNumber), pages ?? undefined)
        .then((detail) => { if (live) { setRoutes(detail.recommended_routes); setRoutesHidden(false); } })
        .catch((failure) => { if (live && isForbidden(failure)) setRoutesHidden(true); });
    }, 400);
    return () => { live = false; window.clearTimeout(timer); };
  }, [client, toNumber, pages]);
  const route = routes[0] ?? null;
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
      client.predictDocument(documentNumber, file)
        .then((answer) => { if (live) setDocumentAnswer(answer); })
        .catch(() => undefined);
    }, 600);
    return () => { live = false; window.clearTimeout(timer); };
  }, [client, documentNumber, file, fileError]);
  // With the document measured, the server names the account and pages the delivery worker would choose (each
  // account priced on its own best pages); the page-count recommendation above is then only a first guess and is
  // never shown with the measured account's price.
  const chosen = documentAnswer?.selected ?? null;
  const documentRoute = (chosen
    ? documentAnswer?.routes.find((item) => item.route === chosen.route)
    : documentAnswer?.routes.find((item) => item.route === routeKey)) ?? null;
  const shown: RoutePrediction | null = documentRoute ?? (chosen ? null : prediction);
  const costSentence = shown ? `What would this cost? ${shown.headline}`
    : (route && !chosen ? routeCostSentence(route, null) : null);
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
      if (!intentRef.current || intentRef.current.destination !== destination || intentRef.current.file !== file) {
        // The same document to the same number after a reload continues that send.
        const fingerprint = sendFingerprint(destination, file!);
        const earlier = loadPendingSend(fingerprint);
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
        maxFileSizeBytes: intent.maxFileSizeBytes, createdAt: intent.createdAt });
      const response = await client.sendFax(intent.destination, intent.file,
        { queueOnly: intent.queueOnly, idempotencyKey: intent.key, sendNow: together !== null && sendNow,
          byCall: route?.route === 'local' && byCall, urgent, coverInHeader, ...sendBody(sendOptions),
          sendBy: sendBy ? new Date(sendBy).toISOString() : undefined, patient: givenPatient });
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
      setFile(null);
      setPatient(NO_PATIENT);
      setPatientAsked(false);
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
          : err instanceof Error ? err.message : "Couldn't confirm the fax was submitted."}${intentRef.current ? ' To retry without creating a duplicate, send the same document to the same number again, even after reloading this page.' : ''}`,
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
              title={faxDisabled ? 'New Queued Test Fax' : 'New Fax Submission'}
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
                  helperText="Before a first fax, Faxbot checks the number against the NPI registry (NPPES)." />
                {recipientCheck?.sentence && (
                  <Alert severity={recipientCheck.warning ? 'warning' : 'info'} data-testid="send-recipient-check">
                    {recipientCheck.sentence}
                  </Alert>
                )}

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

                {(route || chosen) && (
                  <Box data-testid="send-route">
                    <Typography variant="body2">Faxbot will send it through {chosen ? chosen.label : route!.label}.</Typography>
                    {chosen ? (
                      <Typography variant="body2" color="text.secondary" data-testid="send-plan">
                        {documentAnswer!.sentence}
                      </Typography>
                    ) : (
                      <Typography variant="body2" color="text.secondary">{route!.explanation}</Typography>
                    )}
                    {costSentence && (
                      <Typography variant="body2" color="text.secondary" data-testid="send-cost">{costSentence}</Typography>
                    )}
                    {shown && (
                      <Typography variant="caption" color="text.secondary" display="block" data-testid="send-cost-basis">
                        {shown.basis}
                      </Typography>
                    )}
                    {documentRoute?.coding && (
                      <Typography variant="caption" color="text.secondary" display="block" data-testid="send-cost-coding">
                        {documentRoute.coding.sentence}
                      </Typography>
                    )}
                    {shown?.finish_sentence && (
                      <Typography variant="caption" color="text.secondary" display="block" data-testid="send-cost-finish">
                        {shown.finish_sentence}
                      </Typography>
                    )}
                  </Box>
                )}

                <FormControlLabel data-testid="send-urgent"
                  control={<Checkbox checked={urgent} onChange={(e) => setUrgent(e.target.checked)}
                    disabled={!configReady || loading} />}
                  label="Urgent: send before other faxes waiting for the same line" />
                <CoverInHeaderChoice client={client} mailbox={sendOptions.mailbox} checked={coverInHeader}
                  onChange={setCoverInHeader} disabled={!configReady || loading} />

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

                {route?.route === 'local' && (
                  <FormControlLabel data-testid="send-by-call"
                    control={<Checkbox checked={byCall} onChange={(e) => setByCall(e.target.checked)}
                      disabled={!configReady || loading} />}
                    label="Send it through your carrier with a real call instead, to test your fax line" />
                )}

                {together && (
                  <Box data-testid="send-now">
                    <Typography variant="body2" color="text.secondary">{together.sentence}</Typography>
                    <FormControlLabel
                      control={<Checkbox checked={sendNow} onChange={(e) => setSendNow(e.target.checked)}
                        disabled={!configReady || loading} />}
                      label="Send now (faxes already waiting for this number go in the same call)" />
                  </Box>
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
              </Box>
            </Alert>
          </Grow>
        )}

        {/* Instructions */}
        <Fade in timeout={600}>
          <Box sx={{ mt: 4 }}>
            <ResponsiveFormSection
              title="Quick Tips"
              subtitle="Prepare the document and track its job status"
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
