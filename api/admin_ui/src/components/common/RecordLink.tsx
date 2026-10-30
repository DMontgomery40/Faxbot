// The link that opens one record from a list row. It is a real anchor: its
// href is the record's own address, so it can be copied, opened in a new tab
// and reached and used from the keyboard, with a visible focus outline. A
// plain click stays in the console through onOpen; a modified click (Ctrl,
// Cmd, Shift, middle button) follows the href as the browser expects.
import type { MouseEvent, ReactNode } from 'react';
import { Link } from '@mui/material';
import type { SxProps, Theme } from '@mui/material/styles';

export const FOCUS_OUTLINE = {
  '&:focus-visible': {
    outline: '2px solid',
    outlineColor: 'primary.main',
    outlineOffset: 2,
    borderRadius: 0.5,
  },
} as const;

export default function RecordLink({ href, onOpen, ariaLabel, sx, children }: {
  // The record's address, such as '#/faxes/sent?status=failed&fax=…'. Absent when
  // the list is shown without the console's addresses; the link still opens it.
  href?: string;
  onOpen: () => void;
  ariaLabel: string;
  sx?: SxProps<Theme>;
  children: ReactNode;
}) {
  const follow = (event: MouseEvent) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    onOpen();
  };
  return (
    <Link href={href ?? '#'} onClick={follow} aria-label={ariaLabel}
      sx={{ ...FOCUS_OUTLINE, ...(sx as object) }}>
      {children}
    </Link>
  );
}
