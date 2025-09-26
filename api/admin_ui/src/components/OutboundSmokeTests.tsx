import { useState } from 'react';
import {
  Box,
  Typography,
  Card,
  CardContent,
  Button,
  Alert,
  Chip,
  Stack,
  LinearProgress,
  Paper,
  Collapse,
  IconButton,
  Tooltip,
  useTheme,
} from '@mui/material';
import {
  Send as SendIcon,
  Refresh as RefreshIcon,
  ExpandMore as ExpandIcon,
  CheckCircle as SuccessIcon,
  Error as ErrorIcon,
  Warning as WarningIcon,
  Info as InfoIcon,
  ContentCopy as CopyIcon,
  PlayArrow as PlayIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import { useTraits } from '../hooks/useTraits';
import { getProviderDisplayName, getProviderIcon } from '../utils/providerIcons';
import { ResponsiveFormSection } from './common/ResponsiveFormFields';

interface OutboundSmokeTestsProps {
  client: AdminAPIClient;
  canSend?: boolean;
}

interface TestJob {
  id: string;
  type: 'txt' | 'pdf' | 'image';
  status: string;
  error?: string;
  pages?: number;
  created_at: string;
  provider_response?: any;
}

const TEST_TYPES = [
  {
    id: 'txt',
    label: 'Text File',
    description: 'Simple text → PDF conversion test',
    icon: '📄',
  },
  {
    id: 'pdf',
    label: 'PDF File',
    description: 'Direct PDF transmission test',
    icon: '📋',
  },
  {
    id: 'image',
    label: 'Image Test',
    description: 'Generated PDF with graphics test',
    icon: '🖼️',
  },
];

export default function OutboundSmokeTests({ client, canSend = true }: OutboundSmokeTestsProps) {
  const theme = useTheme();
  const { active } = useTraits();

  const [testJobs, setTestJobs] = useState<TestJob[]>([]);
  const [loading, setLoading] = useState<Record<string, boolean>>({});
  const [expandedJobs, setExpandedJobs] = useState<Set<string>>(new Set());

  const activeOutbound = active?.outbound || '';
  const providerName = getProviderDisplayName(activeOutbound);

  const getStatusColor = (status: string): 'success' | 'error' | 'warning' | 'info' | 'default' => {
    switch (status.toLowerCase()) {
      case 'success':
      case 'completed':
      case 'successful':
        return 'success';
      case 'failed':
      case 'error':
        return 'error';
      case 'in_progress':
      case 'queued':
      case 'processing':
        return 'warning';
      default:
        return 'default';
    }
  };

  const getStatusIcon = (status: string) => {
    switch (status.toLowerCase()) {
      case 'success':
      case 'completed':
      case 'successful':
        return <SuccessIcon />;
      case 'failed':
      case 'error':
        return <ErrorIcon />;
      case 'in_progress':
      case 'queued':
      case 'processing':
        return <WarningIcon />;
      default:
        return <InfoIcon />;
    }
  };

  const createTestFile = (type: 'txt' | 'pdf' | 'image'): File => {
    if (type === 'txt') {
      const content = `Faxbot Smoke Test - ${new Date().toISOString()}\n\nProvider: ${providerName}\nTest Type: Text File\n\nThis is a test transmission to verify outbound fax functionality.`;
      const blob = new Blob([content], { type: 'text/plain' });
      return new File([blob], 'faxbot_test.txt', { type: 'text/plain' });
    } else if (type === 'pdf') {
      // Generate a simple PDF
      const pdfContent = generateSimplePdf(`Faxbot Test PDF - ${providerName}`);
      const blob = new Blob([new Uint8Array(pdfContent)], { type: 'application/pdf' });
      return new File([blob], 'faxbot_test.pdf', { type: 'application/pdf' });
    } else {
      // Generate a PDF with graphics (image test)
      const pdfContent = generateGraphicsPdf(`Faxbot Graphics Test - ${providerName}`);
      const blob = new Blob([new Uint8Array(pdfContent)], { type: 'application/pdf' });
      return new File([blob], 'faxbot_image_test.pdf', { type: 'application/pdf' });
    }
  };

  const generateSimplePdf = (text: string): Uint8Array => {
    // Minimal PDF generator for testing
    const enc = (s: string) => new TextEncoder().encode(s);
    const parts: Uint8Array[] = [];
    
    // PDF header
    parts.push(enc('%PDF-1.4\n'));
    
    // Objects
    const o1 = enc('1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n');
    const o2 = enc('2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n');
    const o5 = enc('5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n');
    
    const streamContent = `BT /F1 12 Tf 72 720 Td (${text.replace(/\\/g,'\\\\').replace(/\(/g,'\\(').replace(/\)/g,'\\)')}) Tj ET`;
    const o4Stream = enc(streamContent);
    const o4 = enc(`4 0 obj\n<< /Length ${o4Stream.length} >>\nstream\n`);
    const o4end = enc('\nendstream\nendobj\n');
    const o3 = enc('3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n');
    
    // Assemble with xref
    let offset = 9; // '%PDF-1.4\n' length
    const offsets: number[] = [];
    
    offsets.push(offset);
    parts.push(o1);
    offset += o1.length;
    
    offsets.push(offset);
    parts.push(o2);
    offset += o2.length;
    
    offsets.push(offset);
    parts.push(o3);
    offset += o3.length;
    
    offsets.push(offset);
    parts.push(o4);
    parts.push(o4Stream);
    parts.push(o4end);
    offset += o4.length + o4Stream.length + o4end.length;
    
    offsets.push(offset);
    parts.push(o5);
    offset += o5.length;
    
    const xrefStart = offset;
    const xref = `xref\n0 6\n0000000000 65535 f \n${offsets.map(o=>String(o).padStart(10,'0')+ ' 00000 n ').join('\n')}\ntrailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xrefStart}\n%%EOF`;
    parts.push(enc(xref));
    
    // Combine all parts
    const total = parts.reduce((n,u)=>n+u.length, 0);
    const out = new Uint8Array(total);
    let p = 0;
    for (const u of parts) {
      out.set(u, p);
      p += u.length;
    }
    return out;
  };

  const generateGraphicsPdf = (text: string): Uint8Array => {
    // For now, just generate a simple PDF with text
    // In a real implementation, this could include graphics/shapes
    return generateSimplePdf(`${text}\n\n[Graphics Test - Shapes and lines would appear here]`);
  };

  const runSmokeTest = async (testType: 'txt' | 'pdf' | 'image') => {
    try {
      setLoading(prev => ({ ...prev, [testType]: true }));
      
      const testFile = createTestFile(testType);
      const result = await client.sendFax('+15555550123', testFile);
      
      const newJob: TestJob = {
        id: result.id,
        type: testType,
        status: result.status || 'queued',
        created_at: new Date().toISOString(),
      };
      
      setTestJobs(prev => [newJob, ...prev.slice(0, 9)]); // Keep last 10 tests
      
      // Start polling for status updates
      pollJobStatus(result.id, testType);
      
    } catch (err) {
      const errorJob: TestJob = {
        id: `error-${Date.now()}`,
        type: testType,
        status: 'failed',
        error: err instanceof Error ? err.message : 'Test failed',
        created_at: new Date().toISOString(),
      };
      
      setTestJobs(prev => [errorJob, ...prev.slice(0, 9)]);
    } finally {
      setLoading(prev => ({ ...prev, [testType]: false }));
    }
  };

  const pollJobStatus = async (jobId: string, _testType: string) => {
    let attempts = 0;
    const maxAttempts = 30; // 1 minute of polling
    
    const poll = async () => {
      if (attempts++ > maxAttempts) return;
      
      try {
        const job = await client.getJob(jobId);
        
        setTestJobs(prev => prev.map(testJob => 
          testJob.id === jobId 
            ? {
                ...testJob,
                status: job.status,
                error: job.error,
                pages: job.pages,
                provider_response: job, // Store full job data
              }
            : testJob
        ));
        
        // Continue polling if still in progress
        if (['queued', 'in_progress', 'processing'].includes(job.status.toLowerCase())) {
          setTimeout(poll, 2000);
        }
      } catch (err) {
        // Job might not exist yet, continue polling
        if (attempts < 5) {
          setTimeout(poll, 2000);
        }
      }
    };
    
    // Start polling after a short delay
    setTimeout(poll, 1000);
  };

  const refreshJobStatus = async (jobId: string) => {
    try {
      const job = await client.getJob(jobId);
      setTestJobs(prev => prev.map(testJob => 
        testJob.id === jobId 
          ? {
              ...testJob,
              status: job.status,
              error: job.error,
              pages: job.pages,
              provider_response: job,
            }
          : testJob
      ));
    } catch (err) {
      console.error('Failed to refresh job status:', err);
    }
  };

  const toggleJobExpansion = (jobId: string) => {
    setExpandedJobs(prev => {
      const newSet = new Set(prev);
      if (newSet.has(jobId)) {
        newSet.delete(jobId);
      } else {
        newSet.add(jobId);
      }
      return newSet;
    });
  };

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch (err) {
      const textArea = document.createElement("textarea");
      textArea.value = text;
      document.body.appendChild(textArea);
      textArea.select();
      document.execCommand('copy');
      document.body.removeChild(textArea);
    }
  };

  const formatDate = (dateString: string) => {
    try {
      const date = new Date(dateString);
      return date.toLocaleString();
    } catch {
      return dateString;
    }
  };

  return (
    <ResponsiveFormSection
      title="Outbound Smoke Tests"
      subtitle={`Test ${providerName} transmission with different file types`}
      icon={<PlayIcon />}
    >
      <Stack spacing={3}>
        {/* Provider Info */}
        <Alert severity="info" sx={{ borderRadius: 2 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            {getProviderIcon(activeOutbound)}
            <Typography variant="body2" fontWeight={500}>
              Active Provider: {providerName}
            </Typography>
          </Box>
          <Typography variant="body2">
            Tests use destination +15555550123 (safe test number)
          </Typography>
        </Alert>

        {/* Test Controls */}
        <Card elevation={0} sx={{ border: '1px solid', borderColor: 'divider' }}>
          <CardContent>
            <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 2 }}>
              Run Tests
            </Typography>
            
            <Stack spacing={2}>
              {TEST_TYPES.map(testType => (
                <Box
                  key={testType.id}
                  sx={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    p: 2,
                    border: '1px solid',
                    borderColor: 'divider',
                    borderRadius: 2,
                    backgroundColor: theme.palette.mode === 'dark' 
                      ? 'rgba(255,255,255,0.02)' 
                      : 'rgba(0,0,0,0.02)',
                  }}
                >
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                    <Typography variant="h6">{testType.icon}</Typography>
                    <Box>
                      <Typography variant="subtitle2" fontWeight={600}>
                        {testType.label}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        {testType.description}
                      </Typography>
                    </Box>
                  </Box>
                  
                  <Button
                    variant="outlined"
                    size="small"
                    onClick={() => runSmokeTest(testType.id as 'txt' | 'pdf' | 'image')}
                    disabled={loading[testType.id] || !canSend}
                    startIcon={loading[testType.id] ? undefined : <SendIcon />}
                    sx={{ borderRadius: 1, minWidth: 100 }}
                  >
                    {loading[testType.id] ? 'Sending...' : 'Test'}
                  </Button>
                </Box>
              ))}
            </Stack>
          </CardContent>
        </Card>

        {/* Test Results */}
        {testJobs.length > 0 && (
          <Card elevation={0} sx={{ border: '1px solid', borderColor: 'divider' }}>
            <CardContent>
              <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 2 }}>
                Test Results
              </Typography>
              
              <Stack spacing={2}>
                {testJobs.map(job => {
                  const isExpanded = expandedJobs.has(job.id);
                  const testType = TEST_TYPES.find(t => t.id === job.type);
                  const isInProgress = ['queued', 'in_progress', 'processing'].includes(job.status.toLowerCase());
                  
                  return (
                    <Paper
                      key={job.id}
                      elevation={0}
                      sx={{
                        border: '1px solid',
                        borderColor: job.error ? 'error.main' : 'divider',
                        borderRadius: 2,
                      }}
                    >
                      <Box sx={{ p: 2 }}>
                        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 1 }}>
                          <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                            <Typography variant="h6">{testType?.icon}</Typography>
                            <Box>
                              <Typography variant="subtitle2" fontWeight={600}>
                                {testType?.label} Test
                              </Typography>
                              <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace' }}>
                                {job.id.slice(0, 8)}... • {formatDate(job.created_at)}
                              </Typography>
                            </Box>
                          </Box>
                          
                          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                            <Chip
                              icon={getStatusIcon(job.status)}
                              label={job.status}
                              color={getStatusColor(job.status)}
                              size="small"
                              variant="outlined"
                              sx={{ borderRadius: 1 }}
                            />
                            
                            {!job.id.startsWith('error-') && (
                              <Tooltip title="Refresh Status">
                                <IconButton
                                  size="small"
                                  onClick={() => refreshJobStatus(job.id)}
                                >
                                  <RefreshIcon fontSize="small" />
                                </IconButton>
                              </Tooltip>
                            )}
                            
                            <IconButton
                              size="small"
                              onClick={() => toggleJobExpansion(job.id)}
                            >
                              <ExpandIcon 
                                sx={{ 
                                  transform: isExpanded ? 'rotate(180deg)' : 'none',
                                  transition: 'transform 0.2s'
                                }} 
                              />
                            </IconButton>
                          </Box>
                        </Box>
                        
                        {isInProgress && (
                          <LinearProgress sx={{ mb: 1, borderRadius: 1 }} />
                        )}
                        
                        {job.error && (
                          <Alert severity="error" sx={{ mb: 1, borderRadius: 1 }}>
                            {job.error}
                          </Alert>
                        )}
                        
                        {job.pages && (
                          <Typography variant="caption" color="text.secondary">
                            Pages: {job.pages}
                          </Typography>
                        )}
                        
                        <Collapse in={isExpanded}>
                          <Box sx={{ mt: 2, pt: 2, borderTop: '1px solid', borderColor: 'divider' }}>
                            <Typography variant="caption" color="text.secondary" sx={{ mb: 1, display: 'block' }}>
                              Provider Response:
                            </Typography>
                            
                            {job.provider_response ? (
                              <Paper
                                elevation={0}
                                sx={{
                                  p: 2,
                                  backgroundColor: theme.palette.mode === 'dark' 
                                    ? 'rgba(255,255,255,0.05)' 
                                    : 'rgba(0,0,0,0.05)',
                                  borderRadius: 1,
                                }}
                              >
                                <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1 }}>
                                  <pre style={{
                                    margin: 0,
                                    fontFamily: 'monospace',
                                    fontSize: '0.75rem',
                                    whiteSpace: 'pre-wrap',
                                    wordBreak: 'break-word',
                                    flex: 1,
                                  }}>
                                    {JSON.stringify(job.provider_response, null, 2)}
                                  </pre>
                                  <Tooltip title="Copy Response">
                                    <IconButton
                                      size="small"
                                      onClick={() => copyToClipboard(JSON.stringify(job.provider_response, null, 2))}
                                    >
                                      <CopyIcon fontSize="small" />
                                    </IconButton>
                                  </Tooltip>
                                </Box>
                              </Paper>
                            ) : (
                              <Typography variant="caption" color="text.secondary">
                                No response data available
                              </Typography>
                            )}
                          </Box>
                        </Collapse>
                      </Box>
                    </Paper>
                  );
                })}
              </Stack>
            </CardContent>
          </Card>
        )}
      </Stack>
    </ResponsiveFormSection>
  );
}
