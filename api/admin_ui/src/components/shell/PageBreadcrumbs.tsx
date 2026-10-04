// "Providers › Carrier trunk" above the page; the area opens its first page.
import type React from 'react';
import { Breadcrumbs, Link, Typography } from '@mui/material';
import NavigateNextIcon from '@mui/icons-material/NavigateNext';
import { pageAddress, type NavArea, type NavPage } from '../../navigation';

interface PageBreadcrumbsProps {
  area: NavArea;
  page: NavPage;
  onNavigate: (area: NavArea, page: NavPage) => void;
}

export default function PageBreadcrumbs({ area, page, onNavigate }: PageBreadcrumbsProps) {
  const crumb = (target: NavPage, label: string) => (
    <Link underline="hover" color="inherit" href={pageAddress(area.id, target.id)}
      onClick={(event: React.MouseEvent) => {
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        onNavigate(area, target);
      }}>
      {label}
    </Link>
  );
  const single = area.pages.length === 1 && area.pages[0].id === area.id;
  const groupFirst = page.group ? area.pages.find((candidate) => candidate.group === page.group) : undefined;
  // An area with one page (Overview) has nothing above it; its own heading says where you are.
  if (single) return null;
  return (
    <Breadcrumbs aria-label="You are here" separator={<NavigateNextIcon fontSize="small" />} sx={{ mb: { xs: 1, md: 2 } }}>
      {/* Recipients › Recipients would say the same thing twice. */}
      {page.label !== area.label && crumb(area.pages[0], area.label)}
      {groupFirst && page.group && crumb(groupFirst, page.group)}
      <Typography color="text.primary" aria-current="page">{page.label}</Typography>
    </Breadcrumbs>
  );
}
