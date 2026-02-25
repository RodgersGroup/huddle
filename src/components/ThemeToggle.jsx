import React from 'react';
import { useTheme } from '../context/ThemeContext';

export default function ThemeToggle() {
  const { isDark, toggleTheme } = useTheme();

  return (
    <button
      onClick={toggleTheme}
      role="switch"
      aria-checked={isDark}
      aria-label="Toggle theme"
      style={{
        position: 'relative',
        width: 52,
        height: 28,
        borderRadius: 14,
        border: 'none',
        cursor: 'pointer',
        backgroundColor: isDark ? '#4ecdc4' : '#e4e4e7',
        padding: 0,
        transition: 'background-color 0.2s ease',
        flexShrink: 0,
      }}
    >
      <span
        style={{
          position: 'absolute',
          top: 3,
          left: isDark ? 27 : 3,
          width: 22,
          height: 22,
          borderRadius: '50%',
          backgroundColor: isDark ? '#1a1d27' : '#fff',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 13,
          transition: 'left 0.2s ease, background-color 0.2s ease',
          boxShadow: '0 1px 3px rgba(0,0,0,0.15)',
        }}
        aria-hidden="true"
      >
        {isDark ? '\u{1F319}' : '\u{2600}\u{FE0F}'}
      </span>
    </button>
  );
}
