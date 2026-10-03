import { Alert } from '@mui/material';
import type AdminAPIClient from '../api/client';
import type { AuthMe } from '../api/types';

export default function Groups(_props: { client: AdminAPIClient; me: AuthMe }) {
  return <Alert severity="info">Group management is not available yet.</Alert>;
}
