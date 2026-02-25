import React, { useState } from 'react';
import { useTheme } from '../context/ThemeContext';
import { useMeals } from '../hooks/useMeals';
import ScreenHeader from '../components/ScreenHeader';

export default function MealsScreen({ onBack }) {
  const t = useTheme();
  const { meals, updateMeal } = useMeals();
  const [editing, setEditing] = useState(null); // { day, field }
  const [editValue, setEditValue] = useState('');

  const todayJs = new Date().getDay();
  const todayIdx = todayJs === 0 ? 6 : todayJs - 1;

  const startEdit = (day, field, currentValue) => {
    setEditing({ day, field });
    setEditValue(currentValue || '');
  };

  const saveEdit = () => {
    if (!editing) return;
    updateMeal(editing.day, editing.field, editValue.trim() || null);
    setEditing(null);
    setEditValue('');
  };

  const cancelEdit = () => {
    setEditing(null);
    setEditValue('');
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') saveEdit();
    if (e.key === 'Escape') cancelEdit();
  };

  const isEditing = (day, field) => editing && editing.day === day && editing.field === field;

  const renderSlot = (day, field, value) => {
    if (isEditing(day, field)) {
      return (
        <div>
          <div style={{ fontSize: 10.5, fontWeight: 600, color: t.textFaint, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 4 }}>
            {field === 'lunch' ? 'Lunch' : 'Dinner'}
          </div>
          <input
            autoFocus value={editValue} onChange={(e) => setEditValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={`Add ${field}...`}
            style={{
              width: '100%', padding: '6px 10px', borderRadius: 8,
              border: `1px solid ${t.inputBorder}`, background: t.inputBg,
              color: t.text, fontSize: 13, fontWeight: 500, outline: 'none',
              boxSizing: 'border-box', marginBottom: 6,
            }}
          />
          <div style={{ display: 'flex', gap: 6 }}>
            <button onClick={saveEdit} style={{
              padding: '4px 12px', borderRadius: 6, border: 'none', cursor: 'pointer',
              background: '#4ecdc4', color: '#fff', fontSize: 11, fontWeight: 600,
            }}>Save</button>
            <button onClick={cancelEdit} style={{
              padding: '4px 12px', borderRadius: 6, border: `1px solid ${t.inputBorder}`,
              background: 'transparent', color: t.textMuted, fontSize: 11, fontWeight: 600, cursor: 'pointer',
            }}>Cancel</button>
          </div>
        </div>
      );
    }

    return (
      <div onClick={() => startEdit(day, field, value)} style={{ cursor: 'pointer' }}>
        <div style={{ fontSize: 10.5, fontWeight: 600, color: t.textFaint, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 4 }}>
          {field === 'lunch' ? 'Lunch' : 'Dinner'}
        </div>
        <div style={{ fontSize: 14, fontWeight: 500, color: value ? t.text : t.textFaint }}>
          {value || `+ Add ${field}`}
        </div>
      </div>
    );
  };

  return (
    <div style={{ padding: '0 20px 100px' }}>
      <ScreenHeader title="Meals" onBack={onBack} />

      {meals.map((day, i) => {
        const isToday = i === todayIdx;
        return (
          <div key={day.day} style={{
            background: isToday ? t.todayCardBg : t.mealCardBg,
            border: isToday ? `2px solid ${t.todayBorder}` : `1px solid ${t.cardBorder}`,
            borderRadius: 16, padding: 16, marginBottom: 10,
            boxShadow: isToday ? t.todayCardShadow : t.cardShadow,
            position: 'relative',
          }}>
            {isToday && (
              <div style={{
                position: 'absolute', top: -1, right: 16, background: '#4ecdc4', color: '#fff',
                fontSize: 10, fontWeight: 700, padding: '3px 10px', borderRadius: '0 0 8px 8px',
                letterSpacing: '0.04em', textTransform: 'uppercase',
              }}>Today</div>
            )}
            <div style={{ fontSize: 13, fontWeight: 700, color: isToday ? '#4ecdc4' : t.textMuted, marginBottom: 10, letterSpacing: '0.02em' }}>{day.day}</div>
            <div style={{ display: 'flex', gap: 12 }}>
              <div style={{ flex: 1 }}>
                {renderSlot(day.day, 'lunch', day.lunch)}
              </div>
              <div style={{ width: 1, background: t.divider }} />
              <div style={{ flex: 1 }}>
                {renderSlot(day.day, 'dinner', day.dinner)}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
