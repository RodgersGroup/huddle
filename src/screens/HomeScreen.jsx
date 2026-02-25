import React from 'react';
import { useTheme } from '../context/ThemeContext';
import { useChores } from '../hooks/useChores';
import { useShoppingList } from '../hooks/useShoppingList';
import { MEMBERS, MEMBER_LIST } from '../data/members';
import PersonBadge from '../components/PersonBadge';
import ThemeToggle from '../components/ThemeToggle';

export default function HomeScreen({ onNavigate }) {
  const t = useTheme();
  const { dueToday, overdueCount } = useChores();
  const { itemCount } = useShoppingList();

  const today = new Date();
  const dayName = today.toLocaleDateString('en-AU', { weekday: 'long' });
  const dateStr = today.toLocaleDateString('en-AU', { day: 'numeric', month: 'long' });

  const choresCount = overdueCount + dueToday.length;

  const tiles = [
    { key: 'chores', icon: '\u2713', label: 'Chores', subtitle: `${choresCount} due today`, accent: '#4ecdc4', bg: t.isDark ? '#1a3332' : '#e8faf8' },
    { key: 'calendar', icon: '\u{1F4C5}', label: 'Calendar', subtitle: '3 events', accent: '#7e57c2', bg: t.isDark ? '#2a1f3d' : '#f3eefa' },
    { key: 'meals', icon: '\u{1F37D}', label: 'Meals', subtitle: 'Taco Tuesday', accent: '#ff9800', bg: t.isDark ? '#3d2a0f' : '#fff4e5' },
    { key: 'shopping', icon: '\u{1F6D2}', label: 'Shopping', subtitle: `${itemCount} items`, accent: '#2196f3', bg: t.isDark ? '#0f2440' : '#e8f4fd' },
    { key: 'bills', icon: '$', label: 'Bills', subtitle: '1 due soon', accent: '#ef4444', bg: t.isDark ? '#3b1111' : '#fef2f2' },
    { key: 'tasks', icon: '\u26A1', label: 'Tasks', subtitle: 'All clear', accent: '#22c55e', bg: t.isDark ? '#052e16' : '#f0fdf4' },
  ];

  return (
    <div style={{ padding: '0 20px 100px' }}>
      <div style={{ paddingTop: 56, paddingBottom: 24 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <p style={{ fontSize: 14, color: t.textMuted, margin: 0, fontWeight: 500, letterSpacing: '0.02em' }}>{dayName}</p>
            <h1 style={{ fontSize: 28, fontWeight: 700, color: t.text, margin: '2px 0 0', letterSpacing: '-0.02em' }}>{dateStr}</h1>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <ThemeToggle />
          </div>
        </div>

        {/* Member strip */}
        <div style={{ display: 'flex', gap: 12, marginTop: 16 }}>
          {MEMBER_LIST.map((m) => (
            <div key={m.name} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <PersonBadge name={m.name} size={32} />
              <span style={{ fontSize: 13, fontWeight: 500, color: t.textSecondary }}>{m.name}</span>
            </div>
          ))}
        </div>

        {/* Weather */}
        <div style={{
          marginTop: 16, padding: '12px 16px',
          background: 'linear-gradient(135deg, #4ecdc4 0%, #44a8a0 100%)',
          borderRadius: 14, display: 'flex', alignItems: 'center',
          justifyContent: 'space-between', color: '#fff',
        }}>
          <div>
            <span style={{ fontSize: 13, opacity: 0.9, fontWeight: 500 }}>Shortland</span>
            <div style={{ fontSize: 22, fontWeight: 700, marginTop: 1 }}>24\u00B0C</div>
          </div>
          <div style={{ textAlign: 'right' }}>
            <span style={{ fontSize: 26 }}>{'\u2600\uFE0F'}</span>
            <div style={{ fontSize: 12, opacity: 0.85, fontWeight: 500, marginTop: 2 }}>Sunny, 14-26\u00B0</div>
          </div>
        </div>
      </div>

      {/* Tile Grid */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
        {tiles.map((tile) => (
          <button key={tile.key} onClick={() => onNavigate(tile.key)} style={{
            background: t.card, border: `1px solid ${t.cardBorder}`, borderRadius: 16,
            padding: '20px 16px', textAlign: 'left', cursor: 'pointer',
            transition: 'all 0.15s ease', boxShadow: t.cardShadow,
            position: 'relative', overflow: 'hidden',
          }}
            onMouseDown={(e) => { e.currentTarget.style.transform = 'scale(0.97)'; }}
            onMouseUp={(e) => { e.currentTarget.style.transform = 'scale(1)'; }}
            onMouseLeave={(e) => { e.currentTarget.style.transform = 'scale(1)'; }}
          >
            <div style={{
              width: 40, height: 40, borderRadius: 12, background: tile.bg,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: tile.icon.length === 1 ? 18 : 20, fontWeight: 700,
              color: tile.accent, marginBottom: 12,
            }}>{tile.icon}</div>
            <div style={{ fontSize: 15, fontWeight: 600, color: t.text, marginBottom: 3 }}>{tile.label}</div>
            <div style={{ fontSize: 12.5, color: t.textMuted, fontWeight: 500 }}>{tile.subtitle}</div>
          </button>
        ))}
      </div>

      {/* Up Next */}
      <div style={{ marginTop: 24 }}>
        <h3 style={{ fontSize: 14, fontWeight: 600, color: t.sectionHeader, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 12 }}>Up Next</h3>
        {dueToday.length === 0 ? (
          <div style={{
            background: t.card, border: `1px solid ${t.cardBorder}`, borderRadius: 14,
            padding: '24px 16px', textAlign: 'center', boxShadow: t.cardShadow,
          }}>
            <div style={{ fontSize: 15, fontWeight: 600, color: t.text }}>All caught up!</div>
            <div style={{ fontSize: 13, color: t.textMuted, marginTop: 4 }}>No chores due right now.</div>
          </div>
        ) : (
          dueToday.map((item) => {
            const isDone = item.status === 'completed';
            return (
              <div key={item.id} style={{
                background: t.card, border: `1px solid ${t.cardBorder}`, borderRadius: 14,
                padding: '14px 16px', marginBottom: 10, display: 'flex', alignItems: 'center',
                gap: 14, boxShadow: t.cardShadow,
                opacity: isDone ? 0.55 : 1,
              }}>
                <div style={{
                  width: 36, height: 36, borderRadius: '50%',
                  border: `2.5px solid ${isDone ? '#22c55e' : MEMBERS[item.person]?.color || '#ccc'}`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                  background: isDone ? '#22c55e' : 'transparent',
                  color: isDone ? '#fff' : 'transparent',
                  fontSize: 16, fontWeight: 700,
                }}>{isDone ? '\u2713' : ''}</div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{
                    fontSize: 14.5, fontWeight: 600, color: t.text,
                    textDecoration: isDone ? 'line-through' : 'none',
                  }}>{item.name}</div>
                  <div style={{ fontSize: 12, color: t.textMuted, fontWeight: 500, marginTop: 2 }}>{item.schedule}</div>
                </div>
                <PersonBadge name={item.person} size={28} />
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
