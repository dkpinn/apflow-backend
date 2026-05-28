# Tasks
- if line items extracted tie up to the total of the document, then a green circle should appear on the right of the "Line items" tab, like Supplier when there is a match to a supplier.
- Allow these tracking codes to be mapped to other software, depending on each package's capabilities.
- Add a process flow section for authorisations.
- https://www.ininvoice.com/en/three-way-matching
- Add fall back for AI checking.
- the User Interface needs to update in real time and only allow a user to view the invoice if it has finished all its checks.
- banking for suppliers is not showing up in the master of the supplier.
- searching for supplier in "link to supplier" on invoice details page -> suppliers does not filter down the search when typing for a company.
- if in supplier details the supplier has YES for VAT included in Prices, then it must take the line item price per unit and exclude VAT and only add vat on the subtotal.
- Suppliers main page, but only show active suppliers. There must be a switch to show Archived suppliers that will then refresh the view to include active and inactive suppliers.
- need 2FA to access the program.
- need to state which roles can change which elements of the program.
- Need to list all the elements of the program for SEO or AEO and for Webpage and Advertising.
- Need to add a tax working paper preparation to the program
- Need to create a process flow for automatic authorisation of invoices based on certain criteria. Need to work out what that criteria is.
- need the ability to email the suppliers and ask for company VAT details to be added.
- need to differentiate between cash/card payments and
- need the ability to invoice items from a suppliers invoice to a client.
- need to pickup project numbers and PO numbers on invoices/receipts.
- merge multi imported document pages.
- need to create a parser for when there are multiple invoices / receipts on one page.
- split multi docs on single import
- Stage 6 — GL/Accounting: Entirely missing. expense_account is captured at invoice and line item level, chart of accounts exists, but no journal_entries table, no posting mechanism, and approval writes nothing to any accounting table.
- Priority 3 is the GL integration (significant new feature).
- looking at the invoice page, I am not sure if there are documents being processed or what is in the queue, or the current status of a document.
- line items and extracted data and excl VAT and VAT is not working. Need to understanding why this is not calculating or processing correctly.
- the cropped receipts are not showing the entire cropped area.
- when double clicking the picture to crop, the picture becomes larger than the screen and one cannot the buttons at the bottom.
- merge and re-extract needs to be formatted properly.
- merge and re-extract needs to have a delete option.
- merge and re-extract needs have a drag and drop to order the pages. Possibly change to similar view when extracting documents 3 x 3 view.
- need to add a filter for the drop down when choosing an account code or a dimension. ie when the user starts typing it narrows down the drop down options. The up and down arrow must work to select the correct account. [Not working yet, only when pressing enter does the search start].






- Priority 2 is data completeness (delivery address, supplier create payload, UI clarity). [Done]
- Priority 1 is banking fraud check + atomic approval + wire the Mark as Reviewed button. [Done]
- Add a Dark Mode to the app. [Done]
- Add Account Mappings in the settings section. [Done]
- Include an APPayPal mapping key. [Done]
- Include mapping keys for external accounting packages such as Xero, Sage, QuickBooks, NetSuite, and other supported packages. [Done]
- Add tracking code sections for categories such as departments, countries, and projects. [Done]
- in in supplier details when marking a supplier "Inactive" it needs to update all pages that display that information immediately. [Done]
- under the Invoices section, a button needs to be added re-process. [Done]
- under the Invoice Section next to the invoice ID, the supplier name should appear once the supplier has been confirmed. [Done]
- Change the order of the tabs on the invoice details page to show, Supplier, then Extracted data, then Line Items, then add the banking section under suppliers at the bottom.
- Invoices in accordians must only appear in one list. [Not Done] moved away from accordians. It is not boxes/buckets for the different statuses of uploaded documentation.
- on the supplier master/accounts number, tax number, move the supplier code to a bottom accordian. [Not Done] changed from accordians to a master/document match style. Much easier.
- add supplier is no longer working from the detailed invoice section. [Done]
- drop down box to link supplier is not working. It is showing archived suppliers. [Done]

