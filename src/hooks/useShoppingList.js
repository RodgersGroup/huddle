import { useMemo, useCallback } from 'react';
import { useData } from '../context/DataContext.jsx';

export function useShoppingList() {
  const { state, dispatch } = useData();
  const items = state.shoppingItems;

  const unpurchased = useMemo(
    () => items.filter((i) => !i.purchased),
    [items],
  );

  const purchased = useMemo(
    () => items.filter((i) => i.purchased),
    [items],
  );

  const toggleItem = useCallback(
    (id) => dispatch({ type: 'TOGGLE_SHOPPING_ITEM', id }),
    [dispatch],
  );

  const addItem = useCallback(
    (name, quantity, category) =>
      dispatch({ type: 'ADD_SHOPPING_ITEM', name, quantity, category }),
    [dispatch],
  );

  const deleteItem = useCallback(
    (id) => dispatch({ type: 'DELETE_SHOPPING_ITEM', id }),
    [dispatch],
  );

  const clearPurchased = useCallback(
    () => dispatch({ type: 'CLEAR_PURCHASED' }),
    [dispatch],
  );

  return {
    items,
    unpurchased,
    purchased,
    toggleItem,
    addItem,
    deleteItem,
    clearPurchased,
    itemCount: unpurchased.length,
  };
}
