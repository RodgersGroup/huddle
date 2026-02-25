import React from 'react';
import { useTheme } from '../context/ThemeContext';

const TABS = [
  { key: 'home', icon: '\u2302', label: 'Home' },
  { key: 'chores', icon: '\u2713', label: 'Chores' },
  { key: 'calendar', icon: '\u{1F4C5}', label: 'Calendar' },
  { key: 'meals', icon: '\u{1F37D}', label: 'Meals' },
  { key: 'shopping', icon: '\u{1F6D2}', label: 'Shopping' },
];

export default function BottomNav({ currentScreen, onNavigate }) {
  const theme = useTheme();
  const ACTIVE = '#4ecdc4';

  return (
    <nav
      style={{
        position: 'fixed',
        bottom: 0,
        left: 0,
        right: 0,
        height: 70,
        backgroundColor: theme.card,
        borderTop: `1px solid ${theme.cardBorder}`,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-around',
        paddingBottom: 'env(safe-area-inset-bottom, 0px)',
        zIndex: 100,
      }}
    >
      {TABS.map((tab) => {
        const isActive = currentScreen === tab.key;
        return (
          <button
            key={tab.key}
            onClick={() => onNavigate(tab.key)}
            aria-label={tab.label}
            aria-current={isActive ? 'page' : undefined}
            style={{
              flex: 1,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 2,
              background: 'none',
              border: 'none',
              cursor: 'pointer',
              padding: '8px 0',
              color: isActive ? ACTIVE : theme.textMuted,
              transition: 'color 0.15s ease',
            }}
          >
            <span style={{ fontSize: 20, lineHeight: 1 }} aria-hidden="true">
              {tab.icon}
            </span>
            <span
              style={{
                fontSize: 10,
                fontWeight: isActive ? 700 : 500,
                letterSpacing: '0.02em',
              }}
            >
              {tab.label}
            </span>
          </button>
        );
      })}
    </nav>
  );
}
