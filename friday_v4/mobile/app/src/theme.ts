/**
 * Friday Companion — visual language.
 *
 * A dark "workshop" palette: deep slate, amber accent (the Friday
 * arc-reactor gold), and priority colors that mirror the ambient bus
 * (critical = red, high = amber, normal = green).
 */
export const theme = {
  bg: '#0b0f1a',
  surface: '#121826',
  surfaceAlt: '#1a2233',
  border: '#232d45',
  text: '#e8edf7',
  textDim: '#8b97b5',
  accent: '#f5a623',
  accentText: '#0b0f1a',
  green: '#3ecf8e',
  red: '#ff5c5c',
  amber: '#f5a623',
  blue: '#4d9fff',
  radius: 12,
  radiusSm: 8,
  spacing: 12,
} as const;

export type Priority = 'critical' | 'high' | 'normal' | 'low';

export function priorityColor(priority?: string | null): string {
  switch ((priority ?? '').toLowerCase()) {
    case 'critical':
      return theme.red;
    case 'high':
      return theme.amber;
    case 'normal':
    case 'info':
      return theme.green;
    default:
      return theme.textDim;
  }
}
