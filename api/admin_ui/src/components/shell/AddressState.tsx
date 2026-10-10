// What the console shows in place of a page when its address is not an ordinary one:
// an old address that moved (a one-line notice above the page it opens now), an
// address that names no page, or a page this person may not open. The last two never
// show the page or anything about what the address points to.
import type React from 'react';
import { Alert, Box, Button, Typography } from '@mui/material';
import { plainClick } from './NavPanel';

export function MovedNotice({ was, onClose }: { was: string; onClose: () => void }) {
  return (
    <Alert severity="info" onClose={onClose} data-testid="moved-notice" sx={{ mb: 2 }}>
      You opened an old link to {was}; this is its new home.
    </Alert>
  );
}

const PROBLEMS = {
  unknown: { title: 'Page not found', sentence: 'There is no page at this address.' },
  forbidden: { title: 'Not available to you', sentence: 'Your account does not have access to this page.' },
} as const;

interface AddressProblemProps {
  kind: keyof typeof PROBLEMS;
  // The first page this person may open, offered as the way on.
  home: { label: string; href: string; onOpen: () => void };
}

export function AddressProblem({ kind, home }: AddressProblemProps) {
  const problem = PROBLEMS[kind];
  return (
    <Box data-testid={`address-${kind}`} sx={{ maxWidth: 560, py: { xs: 2, md: 4 } }}>
      <Typography variant="h4" component="h1" gutterBottom>{problem.title}</Typography>
      <Typography variant="body1" color="text.secondary" sx={{ mb: 3 }}>{problem.sentence}</Typography>
      <Button variant="contained" component="a" href={home.href}
        onClick={(event: React.MouseEvent) => {
          if (!plainClick(event)) return;
          event.preventDefault();
          home.onOpen();
        }}>
        Go to {home.label}
      </Button>
    </Box>
  );
}
