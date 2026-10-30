// Find a page or a setting by the words for the job (#53): a search box in the panel
// (Ctrl+K or ⌘K from anywhere) that opens the one place a page or setting lives, with
// its scope and the command that changes it. Results come from GET /auth/finder, the
// same answer `faxbot find` prints, and only ever name pages this person may open.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Box, Button, CircularProgress, Dialog, DialogContent, DialogTitle, List, ListItemButton,
  ListItemText, TextField, Typography,
} from '@mui/material';
import SearchIcon from '@mui/icons-material/Search';
import type AdminAPIClient from '../../api/client';
import type { FinderResult } from '../../api/types';

interface FinderProps {
  client: AdminAPIClient;
  // Open a console address such as '#/delivery/trunk'.
  onOpen: (address: string) => void;
}

export function resultLine(result: FinderResult): string {
  return result.name ? `${result.label} (${result.name})` : result.label;
}

export default function Finder({ client, onOpen }: FinderProps) {
  const [open, setOpen] = useState(false);
  const [words, setWords] = useState('');
  const [results, setResults] = useState<FinderResult[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [active, setActive] = useState(0);
  const asked = useRef(0);

  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setOpen(true);
      }
    };
    window.addEventListener('keydown', shortcut);
    return () => window.removeEventListener('keydown', shortcut);
  }, []);

  useEffect(() => {
    if (!open) return;
    const ticket = ++asked.current;
    setFailed(false);
    const timer = window.setTimeout(() => {
      client.find(words.trim()).then((answer) => {
        if (asked.current !== ticket) return;
        setResults(answer.results);
        setActive(0);
      }).catch(() => {
        if (asked.current === ticket) setFailed(true);
      });
    }, 150);
    return () => window.clearTimeout(timer);
  }, [client, open, words]);

  const choose = useCallback((result: FinderResult | undefined) => {
    if (!result?.address) return;
    setOpen(false);
    setWords('');
    setResults(null);
    onOpen(result.address);
  }, [onOpen]);

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (!results?.length) return;
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive((at) => Math.min(at + 1, results.length - 1)); }
    if (event.key === 'ArrowUp') { event.preventDefault(); setActive((at) => Math.max(at - 1, 0)); }
    if (event.key === 'Enter') { event.preventDefault(); choose(results[active]); }
  };

  return (
    <>
      <Button fullWidth variant="outlined" startIcon={<SearchIcon />} data-testid="finder-open"
        onClick={() => setOpen(true)}
        sx={{ justifyContent: 'flex-start', textTransform: 'none' }}>
        Find a page or setting
        <Box component="kbd" sx={{ ml: 'auto', fontFamily: 'inherit', fontSize: '0.75rem', color: 'text.secondary' }}>Ctrl K</Box>
      </Button>
      <Dialog open={open} onClose={() => setOpen(false)} fullWidth maxWidth="sm" aria-labelledby="finder-title">
        <DialogTitle id="finder-title">Find a page or setting</DialogTitle>
        <DialogContent>
          <TextField autoFocus fullWidth value={words}
            label="Words for the job, or the technical name"
            placeholder="Try “retention”, “T.38” or “who can send”"
            onChange={(event) => setWords(event.target.value)} onKeyDown={onKeyDown}
            inputProps={{ 'data-testid': 'finder-input', role: 'combobox', 'aria-expanded': Boolean(results), 'aria-controls': 'finder-results' }} />
          {failed && <Alert severity="error" sx={{ mt: 2 }}>Could not search. Try again.</Alert>}
          {!failed && results === null && (
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mt: 2 }} role="status">
              <CircularProgress size={18} aria-label="Searching" /><Typography variant="body2">Searching…</Typography>
            </Box>
          )}
          {!failed && results !== null && results.length === 0 && (
            <Typography variant="body2" sx={{ mt: 2 }} role="status">
              Nothing matches. Try the words for the job, such as “retention”, “T.38” or “who can send”.
            </Typography>
          )}
          {!failed && results !== null && results.length > 0 && (
            <List id="finder-results" role="listbox" aria-label="Results" dense sx={{ mt: 1 }}>
              {results.map((result, index) => (
                <ListItemButton key={`${result.kind}-${result.name ?? result.address}-${result.label}`}
                  role="option" aria-selected={index === active} selected={index === active}
                  disabled={!result.address} data-testid={`finder-result-${result.name ?? result.address}`}
                  onMouseEnter={() => setActive(index)} onClick={() => choose(result)}>
                  <ListItemText
                    primary={resultLine(result)}
                    secondary={(
                      <>
                        {result.address ? `${result.place} — ${result.address}` : `Changed in ${result.place}`}
                        <br />Scope: {result.scope}
                        {result.command && <><br />{result.command}</>}
                      </>
                    )} />
                </ListItemButton>
              ))}
            </List>
          )}
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
            Arrow keys move, Enter opens, Esc closes. The same search is on the command line: faxbot find WORDS
          </Typography>
        </DialogContent>
      </Dialog>
    </>
  );
}
