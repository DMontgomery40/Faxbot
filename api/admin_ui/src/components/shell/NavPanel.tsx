// The left navigation panel: Send a fax at the top for people who may send, then the
// six areas, each opening to its pages (long areas under group headings), with the
// person's own menu at the bottom. Every page is a real link to its address.
import { Fragment, useEffect, useState } from 'react';
import type React from 'react';
import { Box, Button, Collapse, List, ListItemButton, ListItemIcon, ListItemText, ListSubheader } from '@mui/material';
import type { SxProps, Theme } from '@mui/material/styles';
import ExpandLessIcon from '@mui/icons-material/ExpandLess';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import SendIcon from '@mui/icons-material/Send';
import { destinationAddress, pageAddress, type NavArea, type NavPage } from '../../navigation';

// Send a fax, kept in view on every page.
export interface SendAction {
  href: string;
  selected: boolean;
  onOpen: () => void;
}

interface NavPanelProps {
  areas: NavArea[];
  // Empty while the address names no page this person may open.
  currentArea: string;
  currentPage: string;
  onNavigate: (area: NavArea, page: NavPage) => void;
  send?: SendAction;
  logo: React.ReactNode;
  footer: React.ReactNode;
}

// A plain click opens the page here; a click with a modifier key (new tab or
// window) is left to the browser, since every item is a link.
export function plainClick(event: React.MouseEvent): boolean {
  return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
}

// The Send a fax link, at the top of the panel and in the phone bar.
export function SendFaxButton({ send, size = 'medium', fullWidth = false, sx }: {
  send: SendAction; size?: 'small' | 'medium'; fullWidth?: boolean; sx?: SxProps<Theme>;
}) {
  return (
    <Button component="a" href={send.href} size={size} fullWidth={fullWidth} sx={sx}
      variant={send.selected ? 'outlined' : 'contained'} startIcon={<SendIcon />}
      aria-current={send.selected ? 'page' : undefined} data-testid="send-action"
      onClick={(event: React.MouseEvent) => {
        if (!plainClick(event)) return;
        event.preventDefault();
        send.onOpen();
      }}>
      Send a fax
    </Button>
  );
}

const itemSx = { borderRadius: 2, mx: 1, mb: 0.25 };

export default function NavPanel({ areas, currentArea, currentPage, onNavigate, send, logo, footer }: NavPanelProps) {
  // The area holding the current page is open and the others are closed; any area
  // can still be opened or closed by hand until the page changes area.
  const [open, setOpen] = useState<Record<string, boolean>>(() => ({ [currentArea]: true }));
  useEffect(() => { setOpen({ [currentArea]: true }); }, [currentArea]);

  const link = (area: NavArea, page: NavPage, nested: boolean, label = page.label, icon = page.icon) => {
    const selected = area.id === currentArea && page.id === currentPage;
    return (
      <ListItemButton key={`${area.id}/${page.id}`} component="a" selected={selected}
        href={page.link ? destinationAddress(page.link) : pageAddress(area.id, page.id)}
        aria-current={selected ? 'page' : undefined}
        onClick={(event: React.MouseEvent) => {
          if (!plainClick(event)) return;
          event.preventDefault();
          onNavigate(area, page);
        }}
        sx={{ ...itemSx, pl: nested ? 4 : 2, py: nested ? 0.5 : 0.75 }}>
        <ListItemIcon sx={{ minWidth: 36 }}>{icon}</ListItemIcon>
        <ListItemText primary={label} primaryTypographyProps={{ variant: 'body2', fontWeight: nested ? 400 : 500 }} />
      </ListItemButton>
    );
  };

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <Box sx={{ px: 2, pt: 2, pb: 1 }}>{logo}</Box>
      {send && (
        <Box sx={{ px: 2, pb: 1.5 }}><SendFaxButton send={send} fullWidth /></Box>
      )}
      <Box component="nav" aria-label="Console" sx={{ flex: 1, overflowY: 'auto' }}>
        <List dense disablePadding>
          {areas.map((area) => {
            // An area with one page (Overview) is a link itself.
            if (area.pages.length === 1 && area.pages[0].id === area.id) return link(area, area.pages[0], false, area.label, area.icon);
            // Pages that keep their address but are not listed (providers not in use, Send a fax) stay out of the panel.
            const listed = area.pages.filter((page) => page.inPanel !== false);
            if (listed.length === 0) return null;
            const expanded = Boolean(open[area.id]);
            const holdsCurrent = area.id === currentArea;
            const groups = [...new Set(listed.map((page) => page.group ?? ''))];
            return (
              <Fragment key={area.id}>
                <ListItemButton onClick={() => setOpen((previous) => ({ ...previous, [area.id]: !expanded }))}
                  aria-expanded={expanded} aria-controls={`nav-${area.id}`} selected={holdsCurrent && !expanded}
                  sx={{ ...itemSx, py: 0.75 }}>
                  <ListItemIcon sx={{ minWidth: 36 }}>{area.icon}</ListItemIcon>
                  <ListItemText primary={area.label} primaryTypographyProps={{ variant: 'body2', fontWeight: 500 }} />
                  {expanded ? <ExpandLessIcon fontSize="small" /> : <ExpandMoreIcon fontSize="small" />}
                </ListItemButton>
                <Collapse in={expanded} timeout="auto" unmountOnExit>
                  <List dense disablePadding id={`nav-${area.id}`}>
                    {groups.map((group) => (
                      <Fragment key={group || 'pages'}>
                        {group && (
                          <ListSubheader disableSticky sx={{ pl: 4, lineHeight: '28px', backgroundColor: 'transparent', fontSize: '0.75rem' }}>
                            {group}
                          </ListSubheader>
                        )}
                        {listed.filter((page) => (page.group ?? '') === group).map((page) => link(area, page, true))}
                      </Fragment>
                    ))}
                  </List>
                </Collapse>
              </Fragment>
            );
          })}
        </List>
      </Box>
      <Box sx={{ borderTop: 1, borderColor: 'divider', p: 1 }}>{footer}</Box>
    </Box>
  );
}
