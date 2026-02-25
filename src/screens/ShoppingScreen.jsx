import React, { useState } from 'react';
import { useTheme } from '../context/ThemeContext';
import { useShoppingList } from '../hooks/useShoppingList';
import ScreenHeader from '../components/ScreenHeader';

export default function ShoppingScreen({ onBack }) {
  const t = useTheme();
  const { unpurchased, purchased, toggleItem, addItem, clearPurchased, itemCount } = useShoppingList();
  const [isAdding, setIsAdding] = useState(false);
  const [newName, setNewName] = useState('');
  const [newQuantity, setNewQuantity] = useState('');
  const [newCategory, setNewCategory] = useState('');

  const handleAdd = () => {
    if (!newName.trim()) return;
    addItem(newName.trim(), newQuantity.trim() || '1', newCategory.trim() || 'General');
    setNewName('');
    setNewQuantity('');
    setNewCategory('');
    setIsAdding(false);
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') handleAdd();
  };

  const handleClear = () => {
    if (window.confirm('Remove all purchased items?')) {
      clearPurchased();
    }
  };

  const inputStyle = {
    padding: '10px 14px', borderRadius: 10,
    border: `1px solid ${t.inputBorder}`, background: t.inputBg,
    color: t.text, fontSize: 14, fontWeight: 500, outline: 'none',
  };

  return (
    <div style={{ padding: '0 20px 100px' }}>
      <ScreenHeader title="Shopping" onBack={onBack}
        rightAction={<span style={{ fontSize: 13, color: t.textMuted, fontWeight: 600 }}>{itemCount} items</span>}
      />

      {/* Add input */}
      {!isAdding ? (
        <div onClick={() => setIsAdding(true)} style={{
          background: t.inputBg, border: `1px solid ${t.inputBorder}`, borderRadius: 14,
          padding: '12px 16px', margin: '12px 0 20px', display: 'flex', alignItems: 'center',
          gap: 12, cursor: 'pointer',
        }}>
          <span style={{ color: t.textFaint, fontSize: 20, fontWeight: 300 }}>+</span>
          <span style={{ color: t.textFaint, fontSize: 14.5, fontWeight: 500 }}>Add an item...</span>
        </div>
      ) : (
        <div style={{
          background: t.card, border: `1px solid ${t.cardBorder}`, borderRadius: 14,
          padding: 16, margin: '12px 0 20px', boxShadow: t.cardShadow,
        }}>
          <input
            autoFocus value={newName} onChange={(e) => setNewName(e.target.value)}
            onKeyDown={handleKeyDown} placeholder="Item name..."
            style={{ ...inputStyle, width: '100%', marginBottom: 10, boxSizing: 'border-box' }}
          />
          <div style={{ display: 'flex', gap: 10 }}>
            <input value={newQuantity} onChange={(e) => setNewQuantity(e.target.value)}
              onKeyDown={handleKeyDown} placeholder="Qty (e.g. 2L)"
              style={{ ...inputStyle, flex: 1 }} />
            <input value={newCategory} onChange={(e) => setNewCategory(e.target.value)}
              onKeyDown={handleKeyDown} placeholder="Category"
              style={{ ...inputStyle, flex: 1 }} />
          </div>
          <div style={{ display: 'flex', gap: 10, marginTop: 10 }}>
            <button onClick={() => setIsAdding(false)} style={{
              flex: 1, padding: '10px', borderRadius: 10, border: `1px solid ${t.inputBorder}`,
              background: 'transparent', color: t.textMuted, fontSize: 14, fontWeight: 600, cursor: 'pointer',
            }}>Cancel</button>
            <button onClick={handleAdd} style={{
              flex: 1, padding: '10px', borderRadius: 10, border: 'none', cursor: 'pointer',
              background: '#2196f3', color: '#fff', fontSize: 14, fontWeight: 600,
              opacity: newName.trim() ? 1 : 0.5,
            }}>Add</button>
          </div>
        </div>
      )}

      {/* Unpurchased items */}
      {unpurchased.map((item) => (
        <div key={item.id} onClick={() => toggleItem(item.id)} style={{
          background: t.card, border: `1px solid ${t.cardBorder}`, borderRadius: 14,
          padding: '14px 16px', marginBottom: 8, display: 'flex', alignItems: 'center',
          gap: 14, cursor: 'pointer', boxShadow: t.cardShadow,
        }}>
          <div style={{ width: 24, height: 24, borderRadius: 7, border: `2px solid ${t.checkBorder}`, flexShrink: 0 }} />
          <div style={{ flex: 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ fontSize: 14.5, fontWeight: 600, color: t.text }}>{item.name}</span>
              {item.inPantry && (
                <span style={{
                  fontSize: 10, fontWeight: 600, color: t.pantryBadgeText, background: t.pantryBadgeBg,
                  padding: '2px 8px', borderRadius: 10, letterSpacing: '0.02em',
                }}>IN PANTRY</span>
              )}
            </div>
            <span style={{ fontSize: 12, color: t.textMuted, fontWeight: 500 }}>{item.quantity} {'\u00B7'} {item.category}</span>
          </div>
        </div>
      ))}

      {/* Purchased section */}
      {purchased.length > 0 && (
        <>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 24, marginBottom: 12 }}>
            <h3 style={{ fontSize: 13, fontWeight: 600, color: t.sectionHeader, textTransform: 'uppercase', letterSpacing: '0.06em', margin: 0 }}>
              Purchased ({purchased.length})
            </h3>
            <button onClick={handleClear} style={{
              background: 'none', border: 'none', color: t.clearBtnColor,
              fontSize: 12.5, fontWeight: 600, cursor: 'pointer',
            }}>Clear all</button>
          </div>
          {purchased.map((item) => (
            <div key={item.id} onClick={() => toggleItem(item.id)} style={{
              background: t.purchasedBg, border: `1px solid ${t.cardBorder}`, borderRadius: 14,
              padding: '12px 16px', marginBottom: 8, display: 'flex', alignItems: 'center',
              gap: 14, cursor: 'pointer', opacity: 0.55,
            }}>
              <div style={{
                width: 24, height: 24, borderRadius: 7, background: '#22c55e',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: '#fff', fontSize: 13, fontWeight: 700, flexShrink: 0,
              }}>{'\u2713'}</div>
              <span style={{ fontSize: 14, fontWeight: 500, color: t.textMuted, textDecoration: 'line-through' }}>{item.name}</span>
            </div>
          ))}
        </>
      )}
    </div>
  );
}
