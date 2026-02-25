import React from 'react';

export default function AddButton({
  accent = '#4ecdc4',
  icon = '+',
  onClick,
  label = 'Add item',
}) {
  return (
    <button
      onClick={onClick}
      aria-label={label}
      style={{
        width: 40,
        height: 40,
        borderRadius: 12,
        border: 'none',
        backgroundColor: accent,
        color: '#fff',
        fontSize: 22,
        fontWeight: 600,
        cursor: 'pointer',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 0,
        lineHeight: 1,
        flexShrink: 0,
      }}
    >
      <span aria-hidden="true">{icon}</span>
    </button>
  );
}
