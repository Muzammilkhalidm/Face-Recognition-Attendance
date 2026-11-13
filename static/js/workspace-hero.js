(function(){
 'use strict';
 var hero=document.querySelector('[data-workspace-hero]'); if(!hero)return;
 var role=document.body.classList.contains('hr')?'hr':(document.body.classList.contains('emp')?'emp':'admin');
 // [title, description, symbol]
 var pages={
  dashboard:      ['One connected workplace','Your daily workspace at a glance.','✦'],
  attendance:     ['Attendance & time tracking','Keep every workday clear, accurate and up to date.','◷'],
  leaves:         ['Leave & holidays','Plan time away and follow every leave request.','☀'],
  holidays:       ['Holidays & time off','See upcoming holidays and important dates.','✿'],
  salary:         ['Salary & payroll','Your compensation details, organised securely.','₨'],
  salaries:       ['Salaries & payroll','Set salaries, generate payroll and record payments.','₨'],
  payroll:        ['Payroll workspace','Review salary records and payroll information.','₨'],
  profile:        ['Your personal profile','Keep your employee information up to date.','◉'],
  account:        ['Your HR account','Manage your profile and account preferences.','◉'],
  tasks:          ['Tasks & projects','Stay focused on priorities, progress and delivery.','✓'],
  projects:       ['Projects','Assign work, follow progress and hit every deadline.','◆'],
  messages:       ['Messages','Clear conversations between your team and management.','✉'],
  announcements:  ['Announcements','Important updates shared across your organisation.','✉'],
  notifications:  ['Notifications centre','Never miss an important workplace update.','♢'],
  employees:      ['People & employees','Manage your team and employee information.','♙'],
  directory:      ['People directory','Find anyone in the organisation in seconds.','♙'],
  department:     ['Your department','The people and updates in your team.','♧'],
  departments:    ['Departments & teams','Keep teams structured and connected.','♧'],
  approvals:      ['Approvals & requests','Review pending actions and keep work moving.','✓'],
  settings:       ['Workspace settings','Configure working hours, rules and security.','⚙'],
  certificates:   ['Certificates & recognition','Celebrate achievements with official certificates.','✶'],
  archive:        ['Deleted employees archive','Ten-year record of every former employee.','▣'],
  records:        ['Full employee records','Every employee detail in one complete register.','▤'],
  audit:          ['Activity log','A transparent record of every administrative change.','≡'],
  admins:         ['Administrators','Manage who can access the admin workspace.','⛨'],
  hr_accounts:    ['HR management','Create and manage HR team access.','⛨'],
  team:           ['HR team access','Temporary HR assignments and permissions.','⛨'],
  advanced_hr:    ['Advanced HR','Bulk reports, imports and HR data exports.','▤'],
  locations:      ['Locations & work from home','Office geofences and remote-work approvals.','⌖'],
  wfh:            ['Work from home','Request and track remote working days.','⌂'],
  shifts:         ['Shifts & schedules','Plan working hours for every team member.','◷'],
  documents:      ['Employee documents','IDs, contracts and records with expiry tracking.','▤'],
  expenses:       ['Expense claims','Review and approve employee expenses.','₨'],
  performance:    ['Performance reviews','Recognise strengths and plan improvements.','★'],
  meeting_alert:  ['Meeting alert','Ring every employee at once, on mobile and web.','♢'],
  recruitment:    ['Recruitment','Open roles and candidates in your pipeline.','✚']
 };
 if(role==='emp'){pages.performance=['My performance','Reviews and feedback shared with you by HR.','★'];pages.leaves=['My leaves','Request time off and track your leave balance.','☀'];pages.messages=['My messages','Updates from admin & HR, and your conversations.','✉'];pages.tasks=['My work & tasks','Your assignments, progress and deadlines.','✓'];pages.attendance=['My attendance','Your check-ins, working hours and history.','◷'];}
 if(role==='hr'){pages.dashboard=['HR workspace','People, attendance and requests at a glance.','✦'];}
 var title=hero.querySelector('[data-hero-title]'),desc=hero.querySelector('[data-hero-description]'),symbol=hero.querySelector('[data-hero-symbol]');
 function show(k){var d=pages[k]||pages.dashboard;if(title)title.textContent=d[0];if(desc)desc.textContent=d[1];if(symbol)symbol.textContent=d[2];}
 show(hero.dataset.section||'dashboard');
})();
