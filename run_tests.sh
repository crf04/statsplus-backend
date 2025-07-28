#!/bin/bash
# Test runner script for NBA backend

echo "🚀 Running NBA Backend Tests"
echo "================================"

# Check if pytest is installed
if ! command -v pytest &> /dev/null; then
    echo "❌ pytest not found. Installing..."
    pip install pytest pytest-mock
fi

# Run different test categories
echo "📦 Running Cache Tests..."
pytest tests/cache/ -v

echo "🔧 Running Service Tests..."  
pytest tests/services/ -v

echo "🛣️  Running Route Tests..."
pytest tests/routes/ -v

echo "🔗 Running Integration Tests..."
pytest tests/integration/ -v

echo "📊 Running All Tests with Coverage..."
pytest tests/ -v --tb=short

echo "✅ Test run complete!"