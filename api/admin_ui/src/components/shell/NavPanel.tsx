// The left navigation panel: the eight areas, each opening to its pages, with
// the person's own menu at the bottom. Every page is a real link to its address.
import { Fragment, useEffect, useState } from 'react';
import type React from 'react';
import { Box, Collapse, List, ListItemButton, ListItemIcon, ListItemText, ListSubheader } from '@mui/material';
import ExpandLessIcon from '@mui/icons-material/ExpandLess';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import { pageAddress, type NavArea, type NavPage } from '../../navigation';

interface NavPanelProps {
  areas: NavArea[];
  currentArea: string;
  currentPage: string;
  onNavigate: (area: NavArea, page: NavPage) => void;
  logo: React.ReactNode;
  footer: React.ReactNode;
}

// A plain click opens the page here; a click with a modifier key (new tab or
// window) is left to the browser, since every item is a link.
function plainClick(event: React.MouseEvent): boolean {
  return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
}

const itemSx = { borderRadius: 2, mx: 1, mb: 0.25 };

export default function NavPanel({ areas, currentArea, currentPage, onNavigate, logo, footer }: NavPanelProps) {
  // The current area starts open; each area opens and closes on its own after that.
  const [open, setOpen] = useState<Record<string, boolean>>(() => ({ [currentArea]: true }));
  useEffect(() => { setOpen((previous) => (previous[currentArea] ? previous : { ...previous, [currentArea]: true })); }, [currentArea]);

  const link = (area: NavArea, page: NavPage, nested: boolean, label = page.label, icon = page.icon) => {
    const selected = area.id === currentArea && page.id === currentPage;
    return (
      <ListItemButton key={`${area.id}/${page.id}`} component="a" href={pageAddress(area.id, page.id)} selected={selected}
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
      <Box component="nav" aria-label="Console" sx={{ flex: 1, overflowY: 'auto' }}>
        <List dense disablePadding>
          {areas.map((area) => {
            // An area with one page (Overview) is a link itself.
            if (area.pages.length === 1 && area.pages[0].id === area.id) return link(area, area.pages[0], false, area.label, area.icon);
            const expanded = Boolean(open[area.id]);
            const holdsCurrent = area.id === currentArea;
            const groups = [...new Set(area.pages.map((page) => page.group ?? ''))];
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
                        {area.pages.filter((page) => (page.group ?? '') === group).map((page) => link(area, page, true))}
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
