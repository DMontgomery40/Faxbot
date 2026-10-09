// Sent → a fax's details: the Direct message or FHIR document it went as, and what came back. Received: the
// Direct messages that arrived, filed into a mailbox or not, with why. Each line is one sentence from Faxbot.
import { useEffect, useState } from 'react';
import { Box, Chip, Typography } from '@mui/material';
import AdminAPIClient, { AdminAPIError } from '../../api/client';
import type { DigitalMessage } from '../../api/digitalTypes';
import { formatServerTime } from '../../api/time';

const STATE_WORDS: Record<string, string> = {
  sending: 'Sending', submitted: 'Waiting for the recipient', processed: 'Accepted by their HISP',
  dispatched: 'Delivered', delivered: 'Delivered', failed: 'Not delivered', uncertain: 'Not known',
  refused: 'Not sent', filed: 'Filed', not_filed: 'Not filed',
};
const GOOD = new Set(['dispatched', 'delivered', 'filed']);
const BAD = new Set(['failed', 'refused', 'not_filed']);
const EVENT_WORDS: Record<string, string> = {
  submitted: 'Your HISP accepted it', processed: "The recipient's HISP accepted it",
  dispatched: "The recipient's system confirmed delivery", delivered: "The recipient's system stored it",
  found: "The recipient's system holds it", failed: 'The recipient said it was not delivered',
  timeout: 'No confirmation came in time', refused: 'Not sent', uncertain: 'The answer was lost',
  not_found: "The recipient's system has no record of it yet", filed: 'Filed into the mailbox',
  mdn_processed: 'Faxbot told the sender it arrived', mdn_final: 'Faxbot told the sender it was filed',
};

// A person without permission to read settings is refused (403); for them there is simply nothing to add here.
// Any other failure says so in one line.
function failure(error: unknown): string | null {
  if (error instanceof AdminAPIError && (error.status === 401 || error.status === 403)) return null;
  return 'Faxbot could not load the Direct and FHIR records just now.';
}

function color(state: string) {
  return GOOD.has(state) ? 'success' : BAD.has(state) ? 'error' : state === 'uncertain' ? 'warning' : 'default';
}

function MessageLine({ message, withEvents }: { message: DigitalMessage; withEvents?: boolean }) {
  return (
    <Box sx={{ mt: 1 }} data-testid="digital-message">
      <Box display="flex" alignItems="center" gap={1} flexWrap="wrap">
        <Typography variant="body2">{message.label}</Typography>
        <Chip size="small" variant="outlined" color={color(message.state)}
          label={STATE_WORDS[message.state] ?? message.state} />
      </Box>
      {message.sentence && <Typography variant="body2" color="text.secondary">{message.sentence}</Typography>}
      {withEvents && (message.events ?? []).filter((event) => EVENT_WORDS[event.kind]).map((event) => (
        <Typography key={`${event.kind}-${event.at}`} variant="caption" color="text.secondary" display="block">
          {`${formatServerTime(event.at)}: ${EVENT_WORDS[event.kind]}`}
        </Typography>
      ))}
    </Box>
  );
}

export function DigitalFaxOutcome({ client, jobId }: { client: AdminAPIClient; jobId: string }) {
  const [messages, setMessages] = useState<DigitalMessage[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    client.getFaxDigitalMessages(jobId).then((loaded) => { if (live) setMessages(loaded.messages); })
      .catch((error) => { if (live) setProblem(failure(error)); });
    return () => { live = false; };
  }, [client, jobId]);
  if (problem) return <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>{problem}</Typography>;
  if (messages.length === 0) return null;
  return (
    <Box sx={{ mb: 2 }} data-testid="digital-fax-outcome">
      <Typography variant="subtitle1" component="h3">Sent without a call</Typography>
      {messages.map((message) => <MessageLine key={message.id} message={message} withEvents />)}
    </Box>
  );
}

export function DigitalReceived({ client }: { client: AdminAPIClient }) {
  const [messages, setMessages] = useState<DigitalMessage[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    client.listDigitalMessages('in').then((loaded) => { if (live) setMessages(loaded.messages); })
      .catch((error) => { if (live) setProblem(failure(error)); });
    return () => { live = false; };
  }, [client]);
  if (problem) return <Typography variant="body2" color="text.secondary" sx={{ mt: 3 }}>{problem}</Typography>;
  if (messages.length === 0) return null;
  return (
    <Box sx={{ mt: 3 }} data-testid="digital-received">
      <Typography variant="h6" component="h2">Direct messages received</Typography>
      <Typography variant="body2" color="text.secondary">
        Documents in each one are filed into the mailbox your HISP account names, like a received fax.
      </Typography>
      {messages.map((message) => (
        <Box key={message.id}>
          <MessageLine message={message} />
          <Typography variant="caption" color="text.secondary">{formatServerTime(message.created_at)}</Typography>
        </Box>
      ))}
    </Box>
  );
}
