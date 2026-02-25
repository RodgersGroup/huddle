import React, { useState, useMemo } from 'react';
import { useTheme } from '../context/ThemeContext';
import { useChores } from '../hooks/useChores';
import { MEMBER_LIST } from '../data/members';
import PersonBadge from '../components/PersonBadge';
import StatusPill from '../components/StatusPill';
import ScreenHeader from '../components/ScreenHeader';
import AddButton from '../components/AddButton';

export default function ChoresScreen({ onBack }) {
  const t = useTheme();
  const { chores, toggleChore, addChore, filtered } = useChores();
  const [filter, setFilter] = useState('all');
  const [showAddForm, setShowAddForm] = useState(false);
  const [newName, setNewName] = useState('');
  const [newPerson, setNewPerson] = useState('');
  const [newSchedule, setNewSchedule] = useState('');

  const statusConfig = {
    overdue: { label: 'Overdue', color: t.overdueColor, bg: t.overdueBg, dot: '#ef4444' },
    due: { label: 'Due Today', color: t.dueColor, bg: t.dueBg, dot: '#3b82f6' },
    completed: { label: 'Done', color: t.completedColor, bg: t.completedBg, dot: '#22c55e' },
    tomorrow: { label: 'Tomorrow', color: t.tomorrowColor, bg: t.tomorrowBg, dot: '#a1a1aa' },
  };

  const filters = [
    { key: 'all', label: 'All' },
    { key: 'due', label: 'Due' },
    { key: 'overdue', label: 'Overdue' },
    { key: 'completed', label: 'Done' },
  ];

  const filteredChores = filtered(filter);

  const completedCount = chores.filter((c) => c.status === 'completed').length;
  const total = chores.length;
  const pct = total > 0 ? Math.round((completedCount / total) * 100) : 0;

  const memberStats = useMemo(() => {
    return MEMBER_LIST.map((m) => {
      const mine = chores.filter((c) => c.person === m.name);
      const done = mine.filter((c) => c.status === 'completed').length;
      return { name: m.name, pct: mine.length > 0 ? Math.round((done / mine.length) * 100) : 0 };
    });
  }, [chores]);

  const handleAdd = () => {
    if (!newName.trim() || !newPerson) return;
    addChore(newName.trim(), newPerson, newSchedule.trim() || 'Once');
    setNewName('');
    setNewPerson('');
    setNewSchedule('');
    setShowAddForm(false);
  };

  const inputStyle = {
    flex: 1, padding: '10px 14px', borderRadius: 10,
    border: `1px solid ${t.inputBorder}`, background: t.inputBg,
    color: t.text, fontSize: 14, fontWeight: 500, outline: 'none',
  };

  return (
    <div style={{ padding: '0 20px 100px' }}>
      <ScreenHeader title="Chores" onBack={onBack}
        rightAction={<AddButton accent="#4ecdc4" onClick={() => setShowAddForm(!showAddForm)} label="Add chore" />}
      />

      {/* Add Form */}
      {showAddForm && (
        <div style={{
          background: t.card, border: `1px solid ${t.cardBorder}`, borderRadius: 16,
          padding: 16, marginBottom: 16, boxShadow: t.cardShadow,
        }}>
          <input
            value={newName} onChange={(e) => setNewName(e.target.value)}
            placeholder="Chore name..."
            style={{ ...inputStyle, width: '100%', marginBottom: 10, boxSizing: 'border-box' }}
          />
          <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
            {MEMBER_LIST.map((m) => (
              <button key={m.name} onClick={() => setNewPerson(m.name)} style={{
                flex: 1, padding: '8px 0', borderRadius: 10, border: 'none', cursor: 'pointer',
                background: newPerson === m.name ? m.color : t.pillBg,
                color: newPerson === m.name ? '#fff' : t.pillText,
                fontSize: 13, fontWeight: 600, transition: 'all 0.15s',
              }}>{m.name}</button>
            ))}
          </div>
          <div style={{ display: 'flex', gap: 10 }}>
            <input
              value={newSchedule} onChange={(e) => setNewSchedule(e.target.value)}
              placeholder="Schedule (e.g. Daily)"
              style={inputStyle}
            />
            <button onClick={handleAdd} style={{
              padding: '10px 20px', borderRadius: 10, border: 'none', cursor: 'pointer',
              background: '#4ecdc4', color: '#fff', fontSize: 14, fontWeight: 600,
              opacity: newName.trim() && newPerson ? 1 : 0.5,
            }}>Add</button>
          </div>
        </div>
      )}

      {/* Scorecard */}
      <div style={{
        background: 'linear-gradient(135deg, #4ecdc4 0%, #38b2ac 100%)',
        borderRadius: 16, padding: '18px 20px', margin: '12px 0 20px',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', color: '#fff',
      }}>
        <div>
          <div style={{ fontSize: 12, fontWeight: 500, opacity: 0.85, letterSpacing: '0.04em', textTransform: 'uppercase' }}>This Week</div>
          <div style={{ fontSize: 32, fontWeight: 800, marginTop: 2, letterSpacing: '-0.02em' }}>{pct}%</div>
          <div style={{ fontSize: 12, opacity: 0.8, fontWeight: 500 }}>{completedCount} of {total} completed</div>
        </div>
        <div style={{ display: 'flex', gap: 12 }}>
          {memberStats.map((m) => (
            <div key={m.name} style={{ textAlign: 'center' }}>
              <PersonBadge name={m.name} size={36} />
              <div style={{ fontSize: 11, fontWeight: 600, marginTop: 4, opacity: 0.9 }}>{m.pct}%</div>
            </div>
          ))}
        </div>
      </div>

      {/* Filters */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
        {filters.map((f) => (
          <button key={f.key} onClick={() => setFilter(f.key)} style={{
            padding: '7px 16px', borderRadius: 20, border: 'none', fontSize: 13,
            fontWeight: 600, cursor: 'pointer', whiteSpace: 'nowrap',
            background: filter === f.key ? t.pillActiveBg : t.pillBg,
            color: filter === f.key ? t.pillActiveText : t.pillText,
            transition: 'all 0.15s',
          }}>{f.label}</button>
        ))}
      </div>

      {/* Chore Cards */}
      {filteredChores.length === 0 ? (
        <div style={{
          textAlign: 'center', padding: '40px 20px', color: t.textMuted,
        }}>
          <div style={{ fontSize: 36, marginBottom: 12 }}>{'\u2713'}</div>
          <div style={{ fontSize: 14, fontWeight: 500 }}>No chores match this filter.</div>
        </div>
      ) : (
        filteredChores.map((chore) => {
          const isDone = chore.status === 'completed';
          const cfg = statusConfig[chore.status] || statusConfig.due;
          return (
            <div key={chore.id} style={{
              background: t.card, border: `1px solid ${t.cardBorder}`, borderRadius: 16,
              padding: 16, marginBottom: 12, boxShadow: t.cardShadow,
              opacity: isDone ? 0.6 : 1, transition: 'all 0.25s ease',
              borderLeft: `4px solid ${cfg.dot}`,
            }}>
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
                <button onClick={() => toggleChore(chore.id)} style={{
                  width: 38, height: 38, borderRadius: '50%',
                  border: `2.5px solid ${isDone ? '#22c55e' : cfg.dot}`,
                  background: isDone ? '#22c55e' : 'transparent',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  cursor: 'pointer', flexShrink: 0, marginTop: 2,
                  transition: 'all 0.2s', color: isDone ? '#fff' : 'transparent',
                  fontSize: 16, fontWeight: 700, padding: 0,
                }}>{'\u2713'}</button>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4, flexWrap: 'wrap' }}>
                    <span style={{ fontSize: 15, fontWeight: 600, color: t.text, textDecoration: isDone ? 'line-through' : 'none' }}>{chore.name}</span>
                    <StatusPill label={cfg.label} color={cfg.color} bg={cfg.bg} />
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 6, flexWrap: 'wrap' }}>
                    <PersonBadge name={chore.person} size={22} />
                    <span style={{ fontSize: 12.5, color: t.textMuted, fontWeight: 500 }}>{chore.person}</span>
                    <span style={{ fontSize: 12, color: t.textFaint }}>{'\u00B7'}</span>
                    <span style={{ fontSize: 12, color: t.textFaint, fontWeight: 500 }}>{chore.schedule}</span>
                    {chore.streak > 0 && (
                      <>
                        <span style={{ fontSize: 12, color: t.textFaint }}>{'\u00B7'}</span>
                        <span style={{ fontSize: 12, color: t.streakColor, fontWeight: 600 }}>{'\u{1F525}'} {chore.streak}</span>
                      </>
                    )}
                  </div>
                </div>
              </div>
              {chore.status === 'overdue' && (
                <div style={{ marginTop: 10, height: 3, background: t.overdueBg, borderRadius: 2, overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: '100%', background: '#ef4444', borderRadius: 2 }} />
                </div>
              )}
            </div>
          );
        })
      )}
    </div>
  );
}
