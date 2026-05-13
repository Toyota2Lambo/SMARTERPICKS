/**
 * Vercel Speed Insights for Vanilla JavaScript
 * Official implementation following Vercel's quickstart guide
 * https://vercel.com/docs/speed-insights/quickstart
 * 
 * This script initializes Speed Insights to track Web Vitals and page performance.
 * Requires Speed Insights to be enabled in your Vercel project dashboard.
 */

// Initialize the Speed Insights queue function
window.si = window.si || function () {
  (window.siq = window.siq || []).push(arguments);
};

// Create and inject the Speed Insights script
(function() {
  const script = document.createElement('script');
  script.src = '/_vercel/speed-insights/script.js';
  script.defer = true;
  
  // Optional: Add error handling for debugging
  script.onerror = function() {
    if (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1') {
      console.log('[Speed Insights] Script not available in local development. Deploy to Vercel to enable Speed Insights.');
    }
  };
  
  document.head.appendChild(script);
})();
