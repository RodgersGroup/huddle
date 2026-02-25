import React, { useState, useMemo } from 'react';
import { useTheme } from '../context/ThemeContext';
import { useCalendar } from '../hooks/useCalendar';
import { MEMBER_LIST } from '../data/members';
import PersonBadge from '../components/PersonBadge';
import ScreenHeader from '../components/ScreenHeader';
import AddButton from '../components/AddButton';

const COLOR_OPTIONS = ['#4ecdc4', '#ff6b9d', '#4caf50', '#7e57c2', '#ff9800', '#2196f3', '#ef4444'];

export default function CalendarScreen({ onBack }) {
  const t = useTheme();
  const { events, addEvent } = useCalendar();
  const [showAddForm, setShowAddForm] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [newTime, setNewTime] = useState('');
  const [newPerson, setNewPerson] = useState('');
  const [newDuration, setNewDuration] = useState('');
  const [newColor, setNewColor] = useState('#4ecdc4');

  // Compute real week days/dates
  const today = new Date();
  const todayDow = today.getDay(); // 0=Sun
  const mondayOffset = todayDow === 0 ? -6 : 1 - todayDow;

  const weekDays = useMemo(() => {
    const labels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    return labels.map((label, i) => {
      const d = new Date(today);
      d.setDate(today.getDate() + mondayOffset + i);
      return { label, date: d.getDate(), isToday: d.toDateString() === today.toDateString() };
    });
  }, []);

  const todayIndex = weekDays.findIndex((d) => d.isToday);
  const [selectedDay, setSelectedDay] = useState(todayIndex >= 0 ? todayIndex : 0);

  const handleAdd = () => {
    if (!newTitle.trim() || !newTime.trim()) return;
    addEvent(newTitle.trim(), newTime.trim(), newPerson || null, newDuration.trim() || '30 min', newColor);
    setNewTitle('');
    setNewTime('');
    setNewPerson('');
    setNewDuration('');
    setNewColor('#4ecdc4');
    setShowAddForm(false);
  };

  const inputStyle = {
    flex: 1, padding: '10px 14px', borderRadius: 10,
    border: `1px solid ${t.inputBorder}`, background: t.inputBg,
    color: t.text, fontSize: 14, fontWeight: 500, outline: 'none',
  };

  return (
    <div style={{ padding: '0 20px 100px' }}>
      <ScreenHeader title="Calendar" onBack={onBack}
        rightAction={<AddButton accent="#7e57c2" onClick={() => setShowAddForm(!showAddForm)} label="Add event" />}
      />

      {/* Add Form */}
      {showAddForm && (
        <div style={{
          background: t.card, border: `1px solid ${t.cardBorder}`, borderRadius: 16,
          padding: 16, marginBottom: 16, boxShadow: t.cardShadow,
        }}>
          <input value={newTitle} onChange={(e) => setNewTitle(e.target.value)}
            placeholder="Event title..." style={{ ...inputStyle, width: '100%', marginBottom: 10, boxSizing: 'border-box' }} />
          <div style={{ display: 'flex', gap: 10, marginBottom: 10 }}>
            <input value={newTime} onChange={(e) => setNewTime(e.target.value)}
              placeholder="Time (e.g. 9:00 AM)" style={inputStyle} />
            <input value={newDuration} onChange={(e) => setNewDuration(e.target.value)}
              placeholder="Duration" style={inputStyle} />
          </div>
          <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
            {MEMBER_LIST.map((m) => (
              <button key={m.name} onClick={() => setNewPerson(newPerson === m.name ? '' : m.name)} style={{
                flex: 1, padding: '8px 0', borderRadius: 10, border: 'none', cursor: 'pointer',
                background: newPerson === m.name ? m.color : t.pillBg,
                color: newPerson === m.name ? '#fff' : t.pillText,
                fontSize: 13, fontWeight: 600, transition: 'all 0.15s',
              }}>{m.name}</button>
            ))}
          </div>
          <div style={{ display: 'flex', gap: 8, marginBottom: 12, alignItems: 'center' }}>
            <span style={{ fontSize: 12, color: t.textMuted, fontWeight: 600 }}>Color:</span>
            {COLOR_OPTIONS.map((c) => (
              <button key={c} onClick={() => setNewColor(c)} style={{
                width: 24, height: 24, borderRadius: '50%', background: c, border: newColor === c ? '3px solid #fff' : 'none',
                boxShadow: newColor === c ? `0 0 0 2px ${c}` : 'none', cursor: 'pointer', padding: 0, flexShrink: 0,
              }} />
            ))}
          </div>
          <button onClick={handleAdd} style={{
            width: '100%', padding: '10px', borderRadius: 10, border: 'none', cursor: 'pointer',
            background: '#7e57c2', color: '#fff', fontSize: 14, fontWeight: 600,
            opacity: newTitle.trim() && newTime.trim() ? 1 : 0.5,
          }}>Add Event</button>
        </div>
      )}

      {/* Week strip */}
      <div style={{ display: 'flex', gap: 6, margin: '16px 0 24px', justifyContent: 'space-between' }}>
        {weekDays.map((day, i) => {
          const isSelected = i === selectedDay;
          return (
            <button key={day.label} onClick={() => setSelectedDay(i)} style={{
              flex: 1, padding: '10px 4px', borderRadius: 14, border: 'none', cursor: 'pointer',
              background: isSelected ? t.pillActiveBg : 'transparent', transition: 'all 0.15s',
            }}>
              <div style={{ fontSize: 11, fontWeight: 600, color: isSelected ? (t.pillActiveText + '99') : t.weekStripDayColor, marginBottom: 4 }}>{day.label}</div>
              <div style={{ fontSize: 16, fontWeight: 700, color: isSelected ? t.pillActiveText : day.isToday ? '#4ecdc4' : t.weekStripDateColor }}>{day.date}</div>
            </button>
          );
        })}
      </div>

      {/* Events timeline */}
      {events.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '40px 20px', color: t.textMuted }}>
          <div style={{ fontSize: 36, marginBottom: 12 }}>{'\u{1F4C5}'}</div>
          <div style={{ fontSize: 14, fontWeight: 500 }}>No events yet.</div>
        </div>
      ) : (
        <div style={{ position: 'relative', paddingLeft: 20 }}>
          <div style={{ position: 'absolute', left: 8, top: 8, bottom: 8, width: 2, background: t.divider, borderRadius: 1 }} />
          {events.map((event) => (
            <div key={event.id} style={{ position: 'relative', marginBottom: 16, paddingLeft: 24 }}>
              <div style={{
                position: 'absolute', left: -16, top: 18, width: 10, height: 10, borderRadius: '50%',
                background: event.color, border: `2px solid ${t.bg}`, boxShadow: `0 0 0 2px ${event.color}33`,
              }} />
              <div style={{ fontSize: 12, color: t.textMuted, fontWeight: 600, marginBottom: 6, letterSpacing: '0.02em' }}>{event.time}</div>
              <div style={{
                background: t.card, border: `1px solid ${t.cardBorder}`, borderRadius: 14,
                padding: '14px 16px', borderLeft: `4px solid ${event.color}`, boxShadow: t.cardShadow,
              }}>
                <div style={{ fontSize: 15, fontWeight: 600, color: t.text, marginBottom: 6 }}>{event.title}</div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  {event.everyone ? (
                    <div style={{ display: 'flex', gap: 2 }}>
                      {MEMBER_LIST.map((m) => <PersonBadge key={m.name} name={m.name} size={20} />)}
                    </div>
                  ) : event.person ? <PersonBadge name={event.person} size={20} /> : null}
                  <span style={{ fontSize: 12, color: t.textMuted, fontWeight: 500 }}>{event.everyone ? 'Everyone' : event.person || ''}</span>
                  <span style={{ fontSize: 12, color: t.textFaint }}>{'\u00B7'}</span>
                  <span style={{ fontSize: 12, color: t.textFaint }}>{event.duration}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
