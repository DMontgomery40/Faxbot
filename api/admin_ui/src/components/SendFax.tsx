import React, { useRef, useState } from 'react';
import {
  Box,
  Typography,
  Button,
  Alert,
  CircularProgress,
  Stack,
  Fade,
  Grow,
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
import AdminAPIClient, { normalizeFaxDestination, reconciliationNotice } from '../api/client';
import type { AdminConfig, FaxSendResult } from '../api/types';
import {
  ResponsiveTextField,
  ResponsiveFileUpload,
  ResponsiveFormSection,
} from './common/ResponsiveFormFields';

interface SendFaxProps {
  client: AdminAPIClient;
  config: AdminConfig | null;
  configLoading: boolean;
  configError: string | null;
}

interface SubmissionIntent {
  key: string;
  destination: string;
  file: File;
  queueOnly: boolean;
  maxFileSizeBytes: number;
}

function submissionKey(): string {
  if (typeof window.crypto?.randomUUID === 'function') return window.crypto.randomUUID();
  if (typeof window.crypto?.getRandomValues !== 'function') {
    throw new Error('Protected fax submission is unavailable in this browser. No request was sent.');
  }
  // getRandomValues remains available for ordinary HTTP self-hosted instances.
  const bytes = window.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function acceptanceMessage(response: FaxSendResult): string {
  switch ((response.delivery_state || response.status).toLowerCase()) {
    case 'held':
      return 'Test fax accepted and held. It will never be automatically transmitted, even after outbound sending is enabled.';
    case 'ready':
      return 'Fax accepted and ready for dispatch. Delivery is not yet confirmed.';
    case 'preparing':
      return 'Fax job is preparing for transmission. Delivery is not yet confirmed.';
    case 'submitting':
      return 'Fax submission is underway. Delivery is not yet confirmed.';
    case 'in_progress':
      return 'Fax transmission is in progress. Delivery is not yet confirmed.';
    case 'success':
    case 'completed':
      return 'Fax job reports successful delivery.';
    case 'failed':
      return 'Fax job failed. Review its details in Jobs before submitting another fax.';
    case 'cancelled':
    case 'canceled':
      return 'Fax job was cancelled.';
    case 'reconciliation_required':
      return reconciliationNotice(response.reconciliation_reason);
    case 'queued':
      return 'Fax accepted and queued. Delivery is not yet confirmed. Check Jobs for the latest status.';
    default:
      return 'Fax request accepted. Check Jobs for its current delivery status.';
  }
}

function SendFax({ client, config, configLoading, configError }: SendFaxProps) {
  const theme = useTheme();
  const isSmallMobile = useMediaQuery(theme.breakpoints.down('sm'));
  
  const [toNumber, setToNumber] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [uploadPickerVersion, setUploadPickerVersion] = useState(0);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ type: 'success' | 'error' | 'warning' | 'info'; message: string; jobId?: string } | null>(null);
  const intentRef = useRef<SubmissionIntent | null>(null);
  const submittingRef = useRef(false);

  // Validation states
  const [toNumberError, setToNumberError] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const configReady = !configLoading && !configError && typeof config?.fax_disabled === 'boolean'
    && Number.isSafeInteger(config.max_file_size_mb) && config.max_file_size_mb > 0;
  const faxDisabled = configReady && config?.fax_disabled === true;
  const maxFileSizeMb = configReady ? config!.max_file_size_mb : null;
  const maxFileSizeBytes = maxFileSizeMb === null ? null : maxFileSizeMb * 1024 * 1024;

  const validatePhone = (number: string): boolean => {
    // Basic validation - allow digits, spaces, dashes, parentheses, and +
    const cleanNumber = normalizeFaxDestination(number);
    if (!cleanNumber) return false;
    if (!/^\+?\d+$/.test(cleanNumber)) return false;
    if (cleanNumber.startsWith('+')) {
      return cleanNumber.length >= 11 && cleanNumber.length <= 15;
    }
    return cleanNumber.length >= 10 && cleanNumber.length <= 15;
  };

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
      setFileError(`The upload limit for this request is ${allowedBytes / (1024 * 1024)} MB. Choose a smaller document.`);
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
        intentRef.current = { key: submissionKey(), destination, file: file!,
          queueOnly: faxDisabled, maxFileSizeBytes: maxFileSizeBytes! };
      }
      const intent = intentRef.current;
      const response = await client.sendFax(intent.destination, intent.file,
        { queueOnly: intent.queueOnly, idempotencyKey: intent.key });
      const state = (response.delivery_state || response.status).toLowerCase();
      setResult({
        type: state === 'reconciliation_required' ? 'warning' : state === 'failed' ? 'error'
          : state === 'success' || state === 'completed' ? 'success' : 'info',
        message: acceptanceMessage(response),
        jobId: response.id,
      });
      
      // Clear form on success
      intentRef.current = null;
      setToNumber('');
      setFile(null);
      setUploadPickerVersion(version => version + 1);
      
    } catch (err) {
      setResult({
        type: 'error',
        message: `${err instanceof Error ? err.message : 'Fax acceptance was not confirmed.'}${intentRef.current ? ' Retrying the unchanged destination and document in this open form reuses the same request. Leaving or reloading starts a new request.' : ''}`,
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
        {faxDisabled ? 'Queue Test Fax' : 'Send Fax'}
      </Typography>

      {configLoading && <Alert severity="info" sx={{ mb: 3 }}>Loading active send configuration…</Alert>}
      {!configLoading && !configReady && <Alert severity="error" sx={{ mb: 3 }}>
        {configError ?? 'Active send configuration is unavailable. Leave and reopen Send to refresh it.'}
      </Alert>}
      {faxDisabled && <Alert severity="warning" sx={{ mb: 3 }}>
        Outbound sending is disabled. Test jobs are held and will never be automatically transmitted, even after sending is enabled.
      </Alert>}

      <Box sx={{ maxWidth: { xs: '100%', md: 800 } }}>
        <Fade in timeout={300}>
          <Box>
            <ResponsiveFormSection
              title={faxDisabled ? 'New Queued Test Fax' : 'New Fax Submission'}
              subtitle={faxDisabled ? 'Queue a PDF or TXT document without transmitting it' : 'Submit a PDF or TXT document to the fax job queue'}
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
                  placeholder="+15551234567"
                  helperText="Enter in E.164 format (+1XXXXXXXXXX) or 10-digit US number"
                  type="tel"
                  disabled={!configReady || loading}
                  required
                  error={toNumberError}
                  errorMessage="Please enter a valid phone number"
                  icon={<PhoneIcon />}
                />

                <ResponsiveFileUpload
                  key={uploadPickerVersion}
                  label="Document to Fax"
                  value={file}
                  onFileSelect={(file) => {
                    if (submittingRef.current) return;
                    if (intentRef.current && intentRef.current.file !== file) intentRef.current = null;
                    setFile(file);
                    setFileError(file && maxFileSizeBytes !== null && file.size > maxFileSizeBytes
                      ? `The active upload limit is ${maxFileSizeMb} MB. Choose a smaller document.`
                      : null);
                  }}
                  accept=".pdf,.txt,application/pdf,text/plain"
                  helperText={maxFileSizeMb === null ? 'PDF or TXT files only. Active upload limit is loading.' : `PDF or TXT files only. New requests: maximum ${maxFileSizeMb} MB. Unchanged retries retain the original limit.`}
                  disabled={!configReady || loading}
                  required
                  error={!!fileError}
                  errorMessage={fileError ?? undefined}
                  icon={<DocumentIcon />}
                />

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
                        setToNumber('');
                        setFile(null);
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
                {result.jobId && (
                  <Box sx={{ mt: 1 }}>
                    <Typography variant="body2" color="text.secondary">
                      Job ID: <strong>{result.jobId}</strong>
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      You can track the status in the Jobs tab
                    </Typography>
                  </Box>
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
              subtitle="Prepare the document and track its job status"
            >
              <Stack spacing={2}>
                <Box>
                  <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 0.5 }}>
                    Phone Number Format
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    • Use E.164 format for international: +1 555 123 4567<br />
                    • US numbers can be entered as: (555) 123-4567 or 5551234567<br />
                    • Avoid extensions or special characters
                  </Typography>
                </Box>

                <Box>
                  <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 0.5 }}>
                    File Requirements
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    • PDF files: Standard documents, forms, letters<br />
                    • TXT files: Plain text will be converted to PDF automatically<br />
                    • Maximum file size for new requests: {maxFileSizeMb === null ? 'unavailable until active settings load' : `${maxFileSizeMb} MB`}<br />
                    • Images: Convert to PDF first using a PDF creator
                  </Typography>
                </Box>

                <Box>
                  <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 0.5 }}>
                    {faxDisabled ? 'Queue-only Test Jobs' : 'Job Status'}
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    {faxDisabled ? <>Test jobs remain held permanently. Enabling sending never automatically transmits them.</> : <>Queue acceptance does not confirm delivery. Delivery timing and status depend on the configured provider.</>}<br />
                    Check the Jobs tab to monitor the accepted job.
                  </Typography>
                </Box>
                <Box>
                  <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 0.5 }}>Retrying a Submission</Typography>
                  <Typography variant="body2" color="text.secondary">
                    Retrying an unchanged destination and the same selected document reuses the request while this form stays open.
                    Unchanged retries also retain the original delivery mode and upload limit.
                    Changing the destination or document, clearing the form, navigating away or reloading starts a new request.
                    Check Jobs before starting another request when acceptance is uncertain.
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
