// A read that failed says so in one short sentence where its data would be, never silence. Someone without
// permission (403) sees nothing there instead, as with AX's views; "nothing there" answers stay quiet.
import { Typography } from '@mui/material';
import { isForbidden } from '../../api/client';

// Whether a failed read should be said: anything but a refusal for lack of permission.
export function saysFailure(failure: unknown): boolean {
  return !isForbidden(failure);
}

export default function LoadFailed({ text, testId }: { text: string; testId?: string }) {
  return (
    <Typography component="span" variant="body2" color="error" display="block" role="status"
      data-testid={testId ?? 'load-failed'}>
      {text}
    </Typography>
  );
}
