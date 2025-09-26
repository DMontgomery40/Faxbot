import React, { useState, useEffect, useRef } from 'react';
import {
  Box,
  Typography,
  Paper,
  Chip,
  List,
  ListItem,
  ListItemText,
  ListItemIcon,
  Divider,
  TextField,
  MenuItem,
  Stack,
  IconButton,
  Tooltip,
  Alert,
  Badge,
  FormControlLabel,
  Switch,
  useTheme,
  useMediaQuery,
} from '@mui/material';
import {
  Circle as CircleIcon,
  Pause as PauseIcon,
  PlayArrow as PlayIcon,
  Clear as ClearIcon,
  Refresh as RefreshIcon,
  FilterList as FilterIcon,
  Timeline as TimelineIcon,
  Event as EventIcon,
  Storage as StorageIcon,
  Memory as MemoryIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import { format } from 'date-fns';

interface Event {
  id: string;
  type: string;
  occurred_at: string;
  provider_id?: string;
  external_id?: string;
  job_id?: string;
  user_id?: string;
  payload_meta?: Record<string, any>;
  correlation_id?: string;
}

interface EventStreamProps {
  client: AdminAPIClient;
}

const EventStream: React.FC<EventStreamProps> = ({ client }) => {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));

  // State
  const [events, setEvents] = useState<Event[]>([]);
  const [eventTypes, setEventTypes] = useState<Array<{ value: string; label: string }>>([]);
  const [isConnected, setIsConnected] = useState(false);
  const [isPaused, setIsPaused] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [selectedEventType, setSelectedEventType] = useState<string>('');
  const [selectedProvider, setSelectedProvider] = useState<string>('');
  const [fromDatabase, setFromDatabase] = useState(false);
  const [limit, setLimit] = useState(50);

  // Refs
  const eventSourceRef = useRef<EventSource | null>(null);
  const eventsRef = useRef<HTMLDivElement>(null);

  // Load initial data
  useEffect(() => {
    loadEventTypes();
    loadRecentEvents();
  }, [selectedEventType, selectedProvider, fromDatabase, limit]);

  // SSE connection management
  useEffect(() => {
    if (!isPaused) {
      connectSSE();
    } else {
      disconnectSSE();
    }

    return () => {
      disconnectSSE();
    };
  }, [isPaused]);

  const loadEventTypes = async () => {
    try {
      const response = await client.getEventTypes();
      setEventTypes(response.event_types);
    } catch (err) {
      console.error('Failed to load event types:', err);
    }
  };

  const loadRecentEvents = async () => {
    setLoading(true);
    setError(null);

    try {
      const response = await client.getRecentEvents({
        limit,
        provider_id: selectedProvider || undefined,
        event_type: selectedEventType || undefined,
        from_db: fromDatabase,
      });

      setEvents(response.events);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load events');
    } finally {
      setLoading(false);
    }
  };

  const connectSSE = () => {
    if (eventSourceRef.current) return;

    try {
      const eventSource = client.createEventSSE();
      eventSourceRef.current = eventSource;

      eventSource.onopen = () => {
        setIsConnected(true);
        setError(null);
      };

      eventSource.addEventListener('connected', () => {
        setIsConnected(true);
      });

      eventSource.addEventListener('event', (e) => {
        try {
          const eventData = JSON.parse(e.data) as Event;

          // Apply filters
          if (selectedEventType && eventData.type !== selectedEventType) return;
          if (selectedProvider && eventData.provider_id !== selectedProvider) return;

          setEvents(prev => [eventData, ...prev.slice(0, limit - 1)]);
        } catch (err) {
          console.error('Failed to parse event:', err);
        }
      });

      eventSource.addEventListener('keepalive', () => {
        // Keepalive received
      });

      eventSource.onerror = () => {
        setIsConnected(false);
        setError('Connection lost to event stream');
        eventSourceRef.current = null;
      };
    } catch (err) {
      setError('Failed to connect to event stream');
    }
  };

  const disconnectSSE = () => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
      setIsConnected(false);
    }
  };

  const clearEvents = () => {
    setEvents([]);
  };

  const getEventIcon = (eventType: string) => {
    if (eventType.includes('fax')) return <EventIcon color="primary" />;
    if (eventType.includes('provider') || eventType.includes('health')) return <TimelineIcon color="secondary" />;
    if (eventType.includes('webhook')) return <CircleIcon color="success" />;
    if (eventType.includes('config')) return <FilterIcon color="info" />;
    return <CircleIcon />;
  };

  const getEventColor = (eventType: string): "default" | "primary" | "secondary" | "error" | "info" | "success" | "warning" => {
    if (eventType.includes('failed') || eventType.includes('error')) return 'error';
    if (eventType.includes('delivered') || eventType.includes('sent')) return 'success';
    if (eventType.includes('queued') || eventType.includes('retrying')) return 'warning';
    if (eventType.includes('health')) return 'info';
    return 'default';
  };

  const formatPayloadMeta = (meta?: Record<string, any>) => {
    if (!meta || Object.keys(meta).length === 0) return null;

    return Object.entries(meta).map(([key, value]) => (
      <Chip
        key={key}
        label={`${key}: ${String(value)}`}
        size="small"
        variant="outlined"
        sx={{ mr: 0.5, mb: 0.5 }}
      />
    ));
  };

  const uniqueProviders = Array.from(new Set(events.map(e => e.provider_id).filter(Boolean)));

  return (
    <Paper sx={{ p: 2, height: isMobile ? 'auto' : '600px', display: 'flex', flexDirection: 'column' }}>
      {/* Header */}
      <Box sx={{ mb: 2, display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
        <Typography variant="h6" sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <EventIcon />
          Event Stream
          <Badge color={isConnected ? 'success' : 'error'} variant="dot">
            <Chip
              size="small"
              label={isConnected ? 'Live' : 'Disconnected'}
              color={isConnected ? 'success' : 'error'}
            />
          </Badge>
        </Typography>

        <Box sx={{ flexGrow: 1 }} />

        {/* Controls */}
        <Stack direction="row" spacing={1} alignItems="center">
          <Tooltip title={isPaused ? 'Resume stream' : 'Pause stream'}>
            <IconButton onClick={() => setIsPaused(!isPaused)} color={isPaused ? 'primary' : 'default'}>
              {isPaused ? <PlayIcon /> : <PauseIcon />}
            </IconButton>
          </Tooltip>

          <Tooltip title="Refresh events">
            <IconButton onClick={loadRecentEvents} disabled={loading}>
              <RefreshIcon />
            </IconButton>
          </Tooltip>

          <Tooltip title="Clear events">
            <IconButton onClick={clearEvents}>
              <ClearIcon />
            </IconButton>
          </Tooltip>
        </Stack>
      </Box>

      {/* Filters */}
      <Stack direction={isMobile ? 'column' : 'row'} spacing={2} sx={{ mb: 2 }}>
        <TextField
          select
          size="small"
          label="Event Type"
          value={selectedEventType}
          onChange={(e) => setSelectedEventType(e.target.value)}
          sx={{ minWidth: 150 }}
        >
          <MenuItem value="">All Types</MenuItem>
          {eventTypes.map((type) => (
            <MenuItem key={type.value} value={type.value}>
              {type.label}
            </MenuItem>
          ))}
        </TextField>

        <TextField
          select
          size="small"
          label="Provider"
          value={selectedProvider}
          onChange={(e) => setSelectedProvider(e.target.value)}
          sx={{ minWidth: 120 }}
        >
          <MenuItem value="">All Providers</MenuItem>
          {uniqueProviders.map((provider) => (
            <MenuItem key={provider} value={provider}>
              {provider}
            </MenuItem>
          ))}
        </TextField>

        <TextField
          type="number"
          size="small"
          label="Limit"
          value={limit}
          onChange={(e) => setLimit(parseInt(e.target.value) || 50)}
          InputProps={{ inputProps: { min: 1, max: 200 } }}
          sx={{ width: 80 }}
        />

        <FormControlLabel
          control={
            <Switch
              checked={fromDatabase}
              onChange={(e) => setFromDatabase(e.target.checked)}
              size="small"
            />
          }
          label={
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
              {fromDatabase ? <StorageIcon fontSize="small" /> : <MemoryIcon fontSize="small" />}
              {fromDatabase ? 'Database' : 'Memory'}
            </Box>
          }
        />
      </Stack>

      {/* Error Alert */}
      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* Events List */}
      <Box
        ref={eventsRef}
        sx={{
          flexGrow: 1,
          overflow: 'auto',
          border: 1,
          borderColor: 'divider',
          borderRadius: 1,
        }}
      >
        {events.length === 0 ? (
          <Box sx={{ p: 4, textAlign: 'center', color: 'text.secondary' }}>
            <EventIcon sx={{ fontSize: 48, mb: 1, opacity: 0.5 }} />
            <Typography variant="body1">
              {loading ? 'Loading events...' : 'No events to display'}
            </Typography>
            <Typography variant="body2">
              {isPaused ? 'Stream is paused' : 'Events will appear here in real-time'}
            </Typography>
          </Box>
        ) : (
          <List dense>
            {events.map((event, index) => (
              <React.Fragment key={event.id}>
                <ListItem alignItems="flex-start">
                  <ListItemIcon sx={{ minWidth: 40 }}>
                    {getEventIcon(event.type)}
                  </ListItemIcon>
                  <ListItemText
                    primary={
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.5 }}>
                        <Chip
                          label={event.type}
                          size="small"
                          color={getEventColor(event.type)}
                          variant="outlined"
                        />
                        {event.provider_id && (
                          <Chip
                            label={event.provider_id}
                            size="small"
                            variant="outlined"
                          />
                        )}
                      </Box>
                    }
                    secondary={
                      <Box>
                        <Typography variant="caption" color="text.secondary" display="block">
                          {format(new Date(event.occurred_at), 'MMM dd, HH:mm:ss.SSS')}
                          {event.job_id && ` • Job: ${event.job_id}`}
                          {event.external_id && ` • Ext: ${event.external_id}`}
                          {event.correlation_id && ` • Correlation: ${event.correlation_id}`}
                        </Typography>
                        {event.payload_meta && (
                          <Box sx={{ mt: 0.5 }}>
                            {formatPayloadMeta(event.payload_meta)}
                          </Box>
                        )}
                      </Box>
                    }
                  />
                </ListItem>
                {index < events.length - 1 && <Divider component="li" />}
              </React.Fragment>
            ))}
          </List>
        )}
      </Box>

      {/* Footer */}
      <Box sx={{ mt: 1, pt: 1, borderTop: 1, borderColor: 'divider' }}>
        <Typography variant="caption" color="text.secondary">
          Showing {events.length} events • Source: {fromDatabase ? 'Database' : 'Memory'} •
          Stream: {isConnected ? 'Connected' : 'Disconnected'}
        </Typography>
      </Box>
    </Paper>
  );
};

export default EventStream;