// Costs → Recommendations: ways to pay less, once Faxbot has some to offer.
import { Box, Paper, Typography } from '@mui/material';
import { ScreenHeader } from '../access/AccessViews';

export const NO_RECOMMENDATIONS = 'Cheaper routes for the numbers you fax and receiving lines you could share will appear here.';

export default function Recommendations() {
  return (
    <Box>
      <ScreenHeader title="Recommendations" />
      <Paper variant="outlined" sx={{ p: 3, borderRadius: 2 }}>
        <Typography variant="body1" color="text.secondary" data-testid="recommendations-empty">{NO_RECOMMENDATIONS}</Typography>
      </Paper>
    </Box>
  );
}
