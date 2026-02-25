export const INITIAL_CHORES = [
  { id: 1, name: 'Empty Dishwasher', person: 'Keiran', status: 'overdue', schedule: 'Daily', streak: 12 },
  { id: 2, name: 'Vacuum Living Room', person: 'Ciara', status: 'due', schedule: 'Mon, Wed, Fri', streak: 5 },
  { id: 3, name: 'Clean Bathroom', person: 'Tahni', status: 'due', schedule: 'Weekly', streak: 3 },
  { id: 4, name: 'Take Out Bins', person: 'Tahni', status: 'completed', schedule: 'Tue, Fri', streak: 8 },
  { id: 5, name: 'Wipe Kitchen Bench', person: 'Keiran', status: 'tomorrow', schedule: 'Daily', streak: 22 },
  { id: 6, name: 'Mop Floors', person: 'Ciara', status: 'tomorrow', schedule: 'Every 3 days', streak: 0 },
];

export const INITIAL_SHOPPING_ITEMS = [
  { id: 1, name: 'Milk', quantity: '2L', category: 'Dairy', purchased: false, inPantry: false },
  { id: 2, name: 'Bread', quantity: '1 loaf', category: 'Bakery', purchased: false, inPantry: true },
  { id: 3, name: 'Chicken Breast', quantity: '500g', category: 'Meat', purchased: false, inPantry: false },
  { id: 4, name: 'Broccoli', quantity: '1 head', category: 'Produce', purchased: false, inPantry: false },
  { id: 5, name: 'Pasta', quantity: '500g', category: 'Pantry', purchased: false, inPantry: true },
  { id: 6, name: 'Eggs', quantity: '12', category: 'Dairy', purchased: true, inPantry: false },
  { id: 7, name: 'Rice', quantity: '1kg', category: 'Pantry', purchased: true, inPantry: false },
];

export const INITIAL_MEALS = [
  { day: 'Monday', lunch: 'Chicken Wraps', dinner: 'Spaghetti Bolognese' },
  { day: 'Tuesday', lunch: 'Leftover Spag Bol', dinner: 'Tacos' },
  { day: 'Wednesday', lunch: 'Sandwiches', dinner: 'Stir Fry' },
  { day: 'Thursday', lunch: null, dinner: 'Fish & Chips' },
  { day: 'Friday', lunch: null, dinner: 'Pizza Night' },
  { day: 'Saturday', lunch: 'Toasties', dinner: null },
  { day: 'Sunday', lunch: 'BBQ', dinner: 'Roast Chicken' },
];

export const INITIAL_EVENTS = [
  { id: 1, time: '8:30 AM', title: 'School Drop-off', person: 'Keiran', duration: '30 min', color: '#4ecdc4' },
  { id: 2, time: '10:00 AM', title: 'Dentist — Ciara', person: 'Ciara', duration: '1 hr', color: '#ff6b9d' },
  { id: 3, time: '3:30 PM', title: 'Swimming Lesson', person: 'Tahni', duration: '45 min', color: '#4caf50' },
  { id: 4, time: '6:00 PM', title: 'Family Dinner', person: null, everyone: true, duration: '1.5 hr', color: '#7e57c2' },
];
