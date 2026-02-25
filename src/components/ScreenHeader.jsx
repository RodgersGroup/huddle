import React from 'react';
import { useTheme } from '../context/ThemeContext';

export default function ScreenHeader({ title, onBack, rightAction }) {
  const theme = useTheme();

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '16px 20px',
        gap: 8,
      }}
    >
      <button
        onClick={onBack}
        aria-label="Back to Home"
        style={{
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          fontSize: 22,
          color: theme.text,
          padding: 0,
          minWidth: 44,
          minHeight: 44,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          borderRadius: 8,
          flexShrink: 0,
        }}
      >
        &#8592;
      </button>

      <h1
        style={{
          flex: 1,
          fontSize: 22,
          fontWeight: 700,
          color: theme.text,
          margin: 0,
          textAlign: 'center',
        }}
      >
        {title}
      </h1>

      <div style={{ minWidth: 44, display: 'flex', justifyContent: 'flex-end' }}>
        {rightAction || null}
      </div>
    </div>
  );
}
