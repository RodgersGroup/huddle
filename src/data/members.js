export const MEMBERS = {
  Keiran: { color: '#4ecdc4', initial: 'K' },
  Ciara: { color: '#ff6b9d', initial: 'C' },
  Tahni: { color: '#4caf50', initial: 'T' },
};

export const MEMBER_LIST = Object.entries(MEMBERS).map(([name, data]) => ({
  name,
  ...data,
}));

export const MEMBER_NAMES = Object.keys(MEMBERS);
