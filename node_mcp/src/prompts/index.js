import { FAXBOT_PROMPTS } from './faxbot.js';

export const PROMPTS = {
  ...FAXBOT_PROMPTS,
};

export function listPrompts() {
  return Object.values(PROMPTS).map((p) => ({
    name: p.name,
    description: p.description,
    arguments: p.arguments,
  }));
}

export function getPrompt(name) {
  return PROMPTS[name];
}

export default { PROMPTS, listPrompts, getPrompt };
