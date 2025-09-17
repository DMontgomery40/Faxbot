import { useState, useEffect } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Chip,
  Button,
  FormControl,
  InputLabel,
  Select,
  MenuItem,
  CircularProgress,
  Alert,
  Grid,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  List,
  ListItem,
  ListItemText,
  Divider,
} from '@mui/material';
import { Refresh as RefreshIcon } from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { FaxJob } from '../api/types';

interface JobsListProps {
  client: AdminAPIClient;
}

function JobsList({ client }: JobsListProps) {
  const [jobs, setJobs] = useState<FaxJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [total, setTotal] = useState(0);
  const [selectedJob, setSelectedJob] = useState<FaxJob | null>(null);
  const [jobDetailOpen, setJobDetailOpen] = useState(false);

  const fetchJobs = async () => {
    try {
      setError(null);
      setLoading(true);
      const params = statusFilter ? { status: statusFilter } : {};
      const data = await client.listJobs(params);
      setJobs(data.jobs);
      setTotal(data.total);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch jobs');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchJobs();
  }, [statusFilter, client]);

  useEffect(() => {
    // Auto-refresh jobs every 10 seconds
    const interval = setInterval(fetchJobs, 10000);
    return () => clearInterval(interval);
  }, [statusFilter, client]);

  const getStatusColor = (status: string): 'success' | 'error' | 'warning' | 'info' | 'default' => {
    switch (status.toLowerCase()) {
      case 'success':
      case 'completed':
        return 'success';
      case 'failed':
      case 'error':
        return 'error';
      case 'queued':
        return 'info';
      case 'in_progress':
      case 'sending':
        return 'warning';
      default:
        return 'default';
    }
  };

  const formatDate = (dateString: string) => {
    try {
      return new Date(dateString).toLocaleString();
    } catch {
      return dateString;
    }
  };

  const handleJobClick = async (jobId: string) => {
    try {
      const jobDetails = await client.getJob(jobId);
      setSelectedJob(jobDetails);
      setJobDetailOpen(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch job details');
    }
  };

  const handleCloseJobDetail = () => {
    setJobDetailOpen(false);
    setSelectedJob(null);
  };

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4" component="h1">
          Fax Jobs
        </Typography>
        <Button
          variant="outlined"
          startIcon={<RefreshIcon />}
          onClick={fetchJobs}
          disabled={loading}
        >
          Refresh
        </Button>
      </Box>

      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} sm={6} md={3}>
          <FormControl fullWidth>
            <InputLabel>Status Filter</InputLabel>
            <Select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              label="Status Filter"
            >
              <MenuItem value="">All Statuses</MenuItem>
              <MenuItem value="queued">Queued</MenuItem>
              <MenuItem value="in_progress">In Progress</MenuItem>
              <MenuItem value="SUCCESS">Success</MenuItem>
              <MenuItem value="FAILED">Failed</MenuItem>
            </Select>
          </FormControl>
        </Grid>
        <Grid item xs={12} sm={6} md={3}>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
            Total: {total} jobs
          </Typography>
        </Grid>
      </Grid>

      {error && (
        <Alert severity="error" sx={{ mb: 3 }}>
          {error}
        </Alert>
      )}

      <Card>
        <CardContent>
          {loading && jobs.length === 0 ? (
            <Box display="flex" justifyContent="center" py={4}>
              <CircularProgress />
            </Box>
          ) : jobs.length === 0 ? (
            <Box textAlign="center" py={4}>
              <Typography variant="body1" color="text.secondary">
                No jobs found
              </Typography>
            </Box>
          ) : (
            <TableContainer sx={{ overflowX: 'auto' }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell sx={{ minWidth: 120 }}>Job ID</TableCell>
                    <TableCell sx={{ minWidth: 100, display: { xs: 'none', sm: 'table-cell' } }}>To Number</TableCell>
                    <TableCell sx={{ minWidth: 80 }}>Status</TableCell>
                    <TableCell sx={{ minWidth: 80, display: { xs: 'none', md: 'table-cell' } }}>Backend</TableCell>
                    <TableCell sx={{ minWidth: 60, display: { xs: 'none', md: 'table-cell' } }}>Pages</TableCell>
                    <TableCell sx={{ minWidth: 150, display: { xs: 'none', lg: 'table-cell' } }}>Error</TableCell>
                    <TableCell sx={{ minWidth: 120 }}>Created</TableCell>
                    <TableCell sx={{ minWidth: 120, display: { xs: 'none', sm: 'table-cell' } }}>Updated</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {jobs.map((job) => (
                    <TableRow 
                      key={job.id} 
                      hover 
                      sx={{ cursor: 'pointer' }}
                      onClick={() => handleJobClick(job.id)}
                    >
                      <TableCell>
                        <Typography variant="body2" fontFamily="monospace" sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {job.id.slice(0, 8)}...
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                        <Typography variant="body2" fontFamily="monospace" sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {job.to_number}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Chip
                          label={job.status}
                          color={getStatusColor(job.status)}
                          size="small"
                          variant="outlined"
                          sx={{ fontSize: { xs: '0.6rem', sm: '0.75rem' } }}
                        />
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>
                        <Typography variant="body2" sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {job.backend}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', md: 'table-cell' } }}>
                        <Typography variant="body2" sx={{ fontSize: { xs: '0.7rem', sm: '0.875rem' } }}>
                          {job.pages || '-'}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ maxWidth: 200, display: { xs: 'none', lg: 'table-cell' } }}>
                        {job.error && (
                          <Typography
                            variant="caption"
                            color="error"
                            sx={{
                              display: 'block',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                              fontSize: { xs: '0.6rem', sm: '0.75rem' }
                            }}
                            title={job.error}
                          >
                            {job.error}
                          </Typography>
                        )}
                      </TableCell>
                      <TableCell>
                        <Typography variant="caption" color="text.secondary" sx={{ fontSize: { xs: '0.6rem', sm: '0.75rem' } }}>
                          {formatDate(job.created_at).split(' ')[0]}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                        <Typography variant="caption" color="text.secondary" sx={{ fontSize: { xs: '0.6rem', sm: '0.75rem' } }}>
                          {formatDate(job.updated_at).split(' ')[0]}
                        </Typography>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          )}
          
          {jobs.length > 0 && (
            <Box mt={2}>
              <Typography variant="caption" color="text.secondary">
                Auto-refreshing every 10 seconds • Phone numbers are masked for HIPAA compliance
              </Typography>
            </Box>
          )}
        </CardContent>
      </Card>

      {/* Job Detail Modal */}
      <Dialog open={jobDetailOpen} onClose={handleCloseJobDetail} maxWidth="md" fullWidth>
        <DialogTitle>
          Job Details
        </DialogTitle>
        <DialogContent>
          {selectedJob && (
            <List>
              <ListItem>
                <ListItemText
                  primary="Job ID"
                  secondary={selectedJob.id}
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="To Number"
                  secondary={selectedJob.to_number}
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="Status"
                  secondary={
                    <Chip
                      label={selectedJob.status}
                      color={getStatusColor(selectedJob.status)}
                      size="small"
                      variant="outlined"
                    />
                  }
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="Backend"
                  secondary={selectedJob.backend}
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="Pages"
                  secondary={selectedJob.pages || 'Unknown'}
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="File Name"
                  secondary={selectedJob.file_name || 'Unknown'}
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="Created"
                  secondary={formatDate(selectedJob.created_at)}
                />
              </ListItem>
              <Divider />
              <ListItem>
                <ListItemText
                  primary="Last Updated"
                  secondary={formatDate(selectedJob.updated_at)}
                />
              </ListItem>
              {selectedJob.error && (
                <>
                  <Divider />
                  <ListItem>
                    <ListItemText
                      primary="Error Details"
                      secondary={
                        <Alert severity="error" sx={{ mt: 1 }}>
                          <Typography variant="body2">
                            {selectedJob.error}
                          </Typography>
                        </Alert>
                      }
                    />
                  </ListItem>
                </>
              )}
            </List>
          )}
        </DialogContent>
        <DialogActions>
          {selectedJob && (
            <Button onClick={async () => {
              try {
                const blob = await client.downloadJobPdf(selectedJob.id);
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `fax_${selectedJob.id}.pdf`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
              } catch (e) {
                // ignore for now; could add error state
              }
            }}>Download PDF</Button>
          )}
          {selectedJob && (
            <Button onClick={async () => {
              try {
                const updated = await client.refreshJob(selectedJob.id);
                setSelectedJob(updated);
              } catch (e) {
                // ignore; could show an alert on failure
              }
            }}>Refresh Status</Button>
          )}
          <Button onClick={handleCloseJobDetail}>Close</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

export default JobsList;
