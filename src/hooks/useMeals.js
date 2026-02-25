import { useCallback } from 'react';
import { useData } from '../context/DataContext.jsx';

export function useMeals() {
  const { state, dispatch } = useData();

  const updateMeal = useCallback(
    (day, field, value) =>
      dispatch({ type: 'UPDATE_MEAL', day, field, value }),
    [dispatch],
  );

  return { meals: state.meals, updateMeal };
}
