/* The first-run flag, read off <body data-first-run> rather than set by an
   inline script. The Content-Security-Policy allows no inline script at all -
   that is what turns any future escaping slip into a blocked request instead
   of a stolen session - so the one value the server has to hand the page
   travels as an attribute, and this file turns it into the global setup.js
   has always read. Loaded after <body> opens, before setup.js. */
window.__FIRST_RUN__ = document.body.dataset.firstRun === 'true';
