import { TextField, type TextFieldProps } from '@mui/material';

// Credentials supplied by the environment (.env) are read at every start; the
// console shows that they are set there and never offers to edit or reveal them.
export const ENV_SET_TEXT = 'Set in .env';
export const ENV_SET_HELP = 'Change it in .env and restart Faxbot.';

/** Names of settings whose value comes from the environment, from GET /admin/settings. */
export function environmentManaged(settings: { _meta?: { [hint: string]: unknown } } | null | undefined): Set<string> {
  const names = settings?._meta?.env_managed;
  return new Set(Array.isArray(names) ? names.filter((name): name is string => typeof name === 'string') : []);
}

type EnvSetFieldProps = Omit<TextFieldProps, 'value' | 'onChange' | 'disabled' | 'type' | 'helperText'> & {
  /** False when the surrounding row already shows the help sentence. */
  withHelp?: boolean;
};

/** A disabled field in place of a credential input, saying the value is set in .env. */
export default function EnvSetField({ withHelp = true, ...props }: EnvSetFieldProps) {
  return <TextField {...props} value={ENV_SET_TEXT} disabled helperText={withHelp ? ENV_SET_HELP : undefined} />;
}
