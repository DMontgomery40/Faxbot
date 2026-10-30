// Help on every page, including sign-in (#53): where you are, how to find the page or
// setting for a job, the documentation, and the steps that get you back in when
// sign-in itself is the problem. Nothing here needs an account or a server answer.
import { useState } from 'react';
import { Button, Dialog, DialogActions, DialogContent, DialogTitle, Link, Typography } from '@mui/material';
import HelpOutlineIcon from '@mui/icons-material/HelpOutline';
import { docsLink } from '../../docsLinks';

interface HelpProps {
  // The installation's documentation address, when the console knows it.
  docsBase?: string;
  // Where the person is, such as 'Delivery setup › Connections › Carrier trunk'.
  place?: string;
  // False on the sign-in and password pages, where there is no search yet.
  signedIn?: boolean;
}

export default function Help({ docsBase, place, signedIn = true }: HelpProps) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button variant="text" startIcon={<HelpOutlineIcon />} data-testid="help-open"
        onClick={() => setOpen(true)} sx={{ textTransform: 'none' }}>
        Help
      </Button>
      <Dialog open={open} onClose={() => setOpen(false)} fullWidth maxWidth="sm" aria-labelledby="help-title">
        <DialogTitle id="help-title">Help</DialogTitle>
        <DialogContent>
          {place && (
            <Typography variant="body2" sx={{ mb: 2 }}>You are on: {place}</Typography>
          )}
          {signedIn && (
            <Typography variant="body2" sx={{ mb: 2 }}>
              Looking for a page or a setting? Choose “Find a page or setting” in the panel, or press
              Ctrl+K (⌘K on a Mac), and type the words for the job or the technical name, such as
              “retention”, “T.38” or “who can send”. It opens the one place that setting is changed.
              The same search runs on the command line as <code>faxbot find WORDS</code>.
            </Typography>
          )}
          <Typography variant="body2" sx={{ mb: 2 }}>
            The guides explain each part of Faxbot, what to set and where:{' '}
            <Link href={docsLink('home', docsBase)} target="_blank" rel="noreferrer">Faxbot documentation</Link>.
          </Typography>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>If you cannot sign in</Typography>
          <Typography variant="body2" sx={{ mb: 1 }}>
            1. Check the address in your browser is this installation's, and try “Sign in with API key”
            if your password is not accepted.
          </Typography>
          <Typography variant="body2" sx={{ mb: 1 }}>
            2. A forgotten password is reset by an owner under Administration › Users. If nobody can
            sign in, sign in on the Faxbot server itself and run <code>faxbot admin recover-owner</code>,
            which creates a new owner account without touching anyone's faxes.
          </Typography>
          <Typography variant="body2" sx={{ mb: 1 }}>
            3. A new installation with no owner yet asks for the installation key: it is API_KEY in
            the installation's .env file on the server.
          </Typography>
          <Typography variant="body2">
            4. “Could not reach the server” means the Faxbot service is stopped or the address is
            wrong: on the server, <code>faxbot admin status</code> says which.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOpen(false)}>Close</Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
