# Swag Labs (SauceDemo) requirements

- Users log in at the home page with a username and password. Valid accounts: `standard_user`, `problem_user`, `locked_out_user`; the shared password is `secret_sauce`.
- Wrong credentials show an error message and keep the user on the login page.
- `locked_out_user` is blocked with a "locked out" error message.
- After login, the Products page lists six products with an image, name, description and price. A sort dropdown can order them by name (A-Z, Z-A) and price (low-high, high-low).
- Each product has an "Add to cart" button that becomes "Remove". The cart icon shows the item count.
- The cart page lists the chosen items with quantity and price, and has "Continue Shopping" and "Checkout" buttons.
- Checkout requires first name, last name and postal code; missing fields show an error. The overview page shows item total, tax and total; "Finish" completes the order with a "Thank you for your order" message.
- A hamburger menu offers All Items, About, Logout and Reset App State. Logout returns to the login page.
