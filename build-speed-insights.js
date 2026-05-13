#!/usr/bin/env node

/**
 * Build script for Vercel Speed Insights
 * 
 * This script ensures that Speed Insights is properly configured
 * across all HTML files in the project.
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Find all HTML files in the root directory
const htmlFiles = fs.readdirSync(__dirname).filter(file => file.endsWith('.html'));

console.log('🚀 Verifying Speed Insights configuration...\n');

let allFilesConfigured = true;

htmlFiles.forEach(file => {
  const filePath = path.join(__dirname, file);
  const content = fs.readFileSync(filePath, 'utf-8');
  
  // Check if the file includes the speed-insights script
  const hasSpeedInsights = content.includes('speed-insights.js');
  
  if (hasSpeedInsights) {
    console.log(`✅ ${file} - Speed Insights configured`);
  } else {
    console.log(`⚠️  ${file} - Missing Speed Insights script`);
    allFilesConfigured = false;
  }
});

console.log('\n');

if (allFilesConfigured) {
  console.log('✅ All HTML files have Speed Insights configured!');
  console.log('📊 Speed Insights will be active after deploying to Vercel.');
  console.log('📖 Enable Speed Insights in your Vercel dashboard: https://vercel.com/dashboard');
  process.exit(0);
} else {
  console.log('⚠️  Some HTML files are missing Speed Insights configuration.');
  console.log('💡 Add the following before </head> in each HTML file:');
  console.log('<script defer src="/speed-insights.js"></script>');
  process.exit(1);
}
