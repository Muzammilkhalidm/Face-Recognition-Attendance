CYBERATTEND — DELETED-EMPLOYEE ARCHIVE (10 YEARS) + PREMIUM THEMES
===================================================================

1) YOUR DELETED DATASET  ->  10-YEAR ARCHIVE
--------------------------------------------
* All 873 face images from your first zip (dataset.zip) are stored in  archived_faces/<employee_id>/
* 24 archive records are in employee_archive.db (retention 3652 days = 10 years, until 2036-11-03):
    - 22 employees from your manifest.csv
    - folder 4 had 50 face images but no manifest row -> kept as "Unnamed employee (ID 4)"
    - the two test accounts that were in the live system (Bilal id 1 and Muzammil id 2) were
      moved into the archive too (with their attendance / leave data) - nothing was lost.
* The OLD dataset that was inside the second zip (dataset_archive/dataset.zip, Bilal's 50 faces)
  was removed: that file is now empty (manifest only). The live system starts clean, and new
  employee ids start from 28 so they can never clash with archived ids.
* Archived faces are NOT mixed into the live dataset/ folder, so the live face recogniser is never
  trained on deleted people.

ADMIN PANEL  (Main admin only)  ->  sidebar: "Deleted Employees (10 yr)"
  /admin/archive                   list + search + department/status filters + counters
  /admin/archive/<id>              full profile, ALL face samples (click to enlarge), every stored
                                   table (attendance, leaves, tasks, payroll...), raw JSON record
  /admin/archive/<id>/faces.zip    one person's faces + record
  /admin/archive/dataset.zip       the whole deleted dataset + manifest.csv
  /admin/archive.csv               archive register (CSV)
  The existing buttons "10-Year Archive DB", "Long-Term Backups" and "Full Employee ZIP" now also
  contain archived_faces/.
When an admin deletes an employee from now on, the same archive is used automatically (that code
already existed) and the page shows them too.

To import another deleted-dataset later:
      python import_deleted_dataset.py path/to/dataset_folder_or_zip
(needs a manifest.csv with employee_id,full_name,username,department,status,samples)

2) THEMES  (Appearance menu, top right of Admin / HR / Employee dashboards)
---------------------------------------------------------------------------
  Light  : original look (+ animations)
  Black  : black UI, every text white, logo LIGHT BLUE
  Blue   : royal-blue UI, every text white, logo BLACK & WHITE
  Purple : violet UI, every text white, logo BLUE
  Red    : crimson base with BLUE / WHITE / PURPLE / BLACK elements, text white
  The chosen theme is applied before the page paints (no light flash) and remembered.

3) ANIMATIONS (all themes)
--------------------------
  page/cards/tables fade-rise in one after another, sidebar items slide in, cards lift and glow on
  hover, buttons lift + shine sweep + click ripple, nav icons wiggle, counters count up, logo mark
  pulses, ambient colour orbs drift on the coloured themes. Respects "reduce motion" OS setting.

Files added/changed:
  static/css/themes-premium.css (new)   static/js/motion.js (new)
  archive_viewer.py (new)  import_deleted_dataset.py (new)
  templates/admin/archive.html, archive_detail.html (new)
  templates/*/base.html, public_base.html (link new css/js)  app.py, enterprise_upgrade.py (small hooks)
Hard-refresh the browser (Ctrl+F5) after starting.

UPDATE 2
--------
* Appearance dropdown now opens ON TOP of the cards and is solid (not see-through).
* Sidebar hover/active items are themed in every panel (Admin, HR, Employee) - no more white box.
* All inputs are themed, including Chrome AUTOFILL (the white name/email fields).
* New notification sound: soft glass-bell chime (static/sounds/notify.wav). To use your own song,
  drop notify.mp3 into static/sounds/ - it is picked automatically.
