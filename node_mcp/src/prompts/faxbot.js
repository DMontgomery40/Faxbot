export const FAXBOT_PROMPTS = {
  faxbot_pdf: {
    name: 'faxbot_pdf',
    description: 'Extract text from PDF and send as fax (avoids base64 token limits)',
    arguments: [
      {
        name: 'pdf_path',
        description: 'Absolute path to PDF file',
        required: true,
      },
      {
        name: 'to',
        description: 'Fax number (E.164 format preferred)',
        required: true,
      },
      {
        name: 'header_text',
        description: 'Optional header text to add',
        required: false,
      },
    ],
  },
};

export default { FAXBOT_PROMPTS };

