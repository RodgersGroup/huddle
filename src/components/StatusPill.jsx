import React from 'react';

export default function StatusPill({ label, color, bg }) {
  return (
    <span
      style={{
        display: 'inline-block',
        fontSize: 11,
        fontWeight: 600,
        textTransform: 'uppercase',
        letterSpacing: '0.03em',
        borderRadius: 20,
        padding: '3px 10px',
        color,
        backgroundColor: bg,
        lineHeight: 1.4,
        whiteSpace: 'nowrap',
      }}
    >
      {label}
    </span>
  );
}
