import React from 'react';
import { MEMBERS } from '../data/members';

export default function PersonBadge({ name, size = 28 }) {
  const member = MEMBERS[name];
  if (!member) return null;

  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: size,
        height: size,
        borderRadius: '50%',
        backgroundColor: member.color,
        color: '#fff',
        fontSize: size * 0.45,
        fontWeight: 700,
        lineHeight: 1,
        flexShrink: 0,
      }}
      aria-label={name}
    >
      {member.initial}
    </span>
  );
}
