import React, { useState } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  TextField,
  Button,
  Alert,
  CircularProgress,
  Grid,
} from '@mui/material';
import { Send as SendIcon } from '@mui/icons-material';
import AdminAPIClient from '../api/client';

interface SendFaxProps {
  client: AdminAPIClient;
}

function SendFax({ client }: SendFaxProps) {
  const [toNumber, setToNumber] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ type: 'success' | 'error'; message: string } | null>(null);

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFile = event.target.files?.[0] || null;
    setFile(selectedFile);
  };

  const handleSend = async () => {
    if (!toNumber.trim() || !file) {
      setResult({
        type: 'error',
        message: 'Please enter a destination number and select a file.',
      });
      return;
    }

    setLoading(true);
    setResult(null);

    try {
      const response = await client.sendFax(toNumber, file);
      setResult({
        type: 'success',
        message: `Fax queued successfully! Job ID: ${response.id} (Status: ${response.status})`,
      });
      
      // Clear form on success
      setToNumber('');
      setFile(null);
      // Reset file input
      const fileInput = document.getElementById('fax-file-input') as HTMLInputElement;
      if (fileInput) fileInput.value = '';
      
    } catch (err) {
      setResult({
        type: 'error',
        message: err instanceof Error ? err.message : 'Failed to send fax',
      });
    } finally {
      setLoading(false);
    }
  };

  return (
    <Box>
      <Typography variant="h4" component="h1" gutterBottom>
        Send Fax
      </Typography>

      <Card sx={{ maxWidth: { xs: '100%', sm: 600 } }}>
        <CardContent sx={{ p: { xs: 2, sm: 3 } }}>
          <Typography variant="h6" gutterBottom sx={{ fontSize: { xs: '1.1rem', sm: '1.25rem' } }}>
            Test Fax Transmission
          </Typography>
          
          <Grid container spacing={{ xs: 2, sm: 3 }}>
            <Grid item xs={12}>
              <TextField
                fullWidth
                label="Destination Number"
                value={toNumber}
                onChange={(e) => setToNumber(e.target.value)}
                placeholder="+15551234567"
                helperText="Enter in E.164 format (+1XXXXXXXXXX) or just digits"
                size="small"
              />
            </Grid>
            
            <Grid item xs={12}>
              <input
                id="fax-file-input"
                type="file"
                accept=".pdf,.txt,application/pdf,text/plain"
                onChange={handleFileChange}
                style={{ display: 'none' }}
              />
              <label htmlFor="fax-file-input">
                <Button
                  variant="outlined"
                  component="span"
                  fullWidth
                  sx={{ 
                    height: { xs: 48, sm: 56 }, 
                    justifyContent: 'flex-start', 
                    textAlign: 'left',
                    fontSize: { xs: '0.875rem', sm: '1rem' }
                  }}
                >
                  {file ? (file.name.length > 30 ? file.name.slice(0, 30) + '...' : file.name) : 'Select File (PDF or TXT)'}
                </Button>
              </label>
              <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: 'block', fontSize: { xs: '0.7rem', sm: '0.75rem' } }}>
                Supported formats: PDF, TXT
              </Typography>
            </Grid>
            
            <Grid item xs={12}>
              <Button
                fullWidth
                variant="contained"
                size="large"
                startIcon={loading ? <CircularProgress size={20} /> : <SendIcon />}
                onClick={handleSend}
                disabled={loading || !toNumber.trim() || !file}
              >
                {loading ? 'Sending...' : 'Send Fax'}
              </Button>
            </Grid>
            
            {result && (
              <Grid item xs={12}>
                <Alert severity={result.type}>
                  {result.message}
                </Alert>
              </Grid>
            )}
          </Grid>
        </CardContent>
      </Card>

      <Alert severity="info" sx={{ mt: 3, maxWidth: 600 }}>
        <Typography variant="body2">
          <strong>Test Fax Notes:</strong>
        </Typography>
        <ul style={{ margin: '8px 0', paddingLeft: '20px' }}>
          <li>This sends an actual fax through your configured backend</li>
          <li>Charges may apply depending on your provider</li>
          <li>Use a test number you control to verify functionality</li>
          <li>Job status can be monitored in the Jobs tab</li>
        </ul>
      </Alert>
    </Box>
  );
}

export default SendFax;
