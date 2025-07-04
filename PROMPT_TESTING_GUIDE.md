# 🏀 NBA Natural Language Query - Prompt Testing Guide

This guide shows you all the different ways to test your natural language prompts with the NBA query parsing system.

## 🚀 Quick Start

### 1. Interactive Testing (Recommended for exploration)
```bash
python test_prompts.py
```

Enter queries interactively and see results in real-time:
```
💬 Enter your query: LeBron James last 10 games
============================================================
🔍 Query: 'LeBron James last 10 games'
============================================================
👤 Player: LeBron James
⏰ Time Period: recent
🎯 Game Count: 10
🎯 Intent: game_logs
📊 Confidence: 0.80
💯 Confidence Level: 🟢 High (Excellent)
```

**Interactive Commands:**
- Type any NBA query to test
- Type `examples` to see sample queries
- Type `quit`, `exit`, or `q` to stop

### 2. Batch Testing (Test many queries at once)
```bash
python test_prompts.py --batch
```

Runs 27 predefined test cases covering:
- ✅ Basic player queries
- ✅ Time-based queries  
- ✅ Location queries (home/away)
- ✅ Opponent analysis
- ✅ Player profiles
- ✅ Team queries
- ✅ Edge cases

### 3. File-Based Testing (Test from your own file)
```bash
python test_prompts.py --file sample_queries.txt
```

Test 55+ queries from the included sample file, or create your own!

## 📝 Creating Your Own Test Files

Create a text file with one query per line:

```txt
# My Custom NBA Queries
# Lines starting with # are comments

LeBron James last 10 games
Stephen Curry this season
Giannis at home
Kevin Durant vs top defenses
```

Then test with:
```bash
python test_prompts.py --file my_queries.txt
```

## 🧪 Advanced Testing with Custom Test Cases

### Using the Custom Test Framework

```bash
python tests/test_custom_prompts.py
```

### Adding Your Own Test Cases

Edit `tests/test_custom_prompts.py` and add your test cases:

```python
def test_my_custom_queries(self):
    """Test my specific use cases"""
    test_cases = [
        CustomPromptTestCase(
            "Your query here",
            expected_player="Expected Player",
            expected_intent="game_logs",
            expected_time_period="recent",
            expected_game_count=10,
            min_confidence=0.6,
            description="What this test validates"
        ),
        # Add more test cases...
    ]
    
    results = self.run_custom_test_cases(test_cases)
    self.print_test_results("My Custom Tests", results)
```

## 📊 Understanding Test Results

### Confidence Levels
- 🟢 **High (0.8+)**: Excellent understanding, ready for production
- 🟡 **Medium (0.6-0.79)**: Good understanding, minor improvements possible
- 🟠 **Low (0.4-0.59)**: Needs improvement, unclear parsing
- 🔴 **Very Low (0.0-0.39)**: Poor understanding, major issues

### Parsed Components
- 👤 **Player**: Recognized player name
- 🏀 **Team**: Recognized team
- ⏰ **Time Period**: season, recent, month, etc.
- 🎯 **Game Count**: Number of specific games
- 🏠 **Location**: home, away, both
- 🛡️ **Opponent Filters**: Against specific team types
- 🎯 **Intent**: game_logs, player_profile, team_stats

## 🎯 Testing Different Query Types

### 1. Player Performance Queries
```
✅ "LeBron James last 10 games"
✅ "Stephen Curry this season"
✅ "Giannis recent performance"
```

### 2. Time-Based Queries
```
✅ "Kevin Durant past 15 games"
✅ "Jayson Tatum this month"
✅ "Luka Doncic recent stats"
```

### 3. Location Queries
```
✅ "Jimmy Butler at home"
✅ "Damian Lillard on the road"
✅ "Paul George away games"
```

### 4. Opponent Analysis
```
✅ "James Harden against top 10 defenses"
✅ "Devin Booker vs elite teams"
✅ "Trae Young against worst rebounding teams"
```

### 5. Player Profile Queries
```
✅ "How does Nikola Jokic play?"
✅ "Joel Embiid playing style"
✅ "Kawhi Leonard strengths"
```

### 6. Conversational Queries
```
✅ "Tell me about LeBron James performance"
✅ "Show me how Stephen Curry has been playing"
✅ "What are Giannis stats recently?"
```

### 7. Complex Multi-Component Queries
```
✅ "LeBron James last 10 home games against top defenses"
✅ "Stephen Curry this season on the road vs elite teams"
✅ "Kevin Durant away games this season"
```

## 🔧 Troubleshooting Common Issues

### Low Confidence Scores
1. **Add more context**: "LeBron last game" → "LeBron James last 10 games"
2. **Use full names**: "Curry" → "Stephen Curry"
3. **Be specific**: "stats" → "shooting stats this season"

### Player Not Recognized
1. **Check spelling**: "Giannos" → "Giannis"
2. **Use full name**: "Greek Freak" → "Giannis Antetokounmpo"
3. **Check player database**: Player might not be in test data

### Intent Misclassification
1. **Use clear keywords**: 
   - Game logs: "games", "performance", "stats"
   - Player profile: "how does", "playing style", "strengths"
   - Team stats: "team", "defense", "offense"

## 📈 Performance Benchmarks

Based on the sample queries (55 total):
- **Average Confidence**: 0.49
- **High Confidence (≥0.6)**: 36/55 (65.5%)
- **Best Query Type**: Player + Time + Location (0.90 confidence)
- **Challenging Areas**: Partial names, generic queries

## 🎓 Best Practices

### For High Accuracy:
1. **Use full player names**: "LeBron James" not "LeBron"
2. **Be specific with time**: "last 10 games" not "recently"
3. **Clear intent**: "shooting stats" not just "stats"
4. **Complete sentences**: "Stephen Curry this season at home"

### For Testing:
1. **Start simple**: Test basic queries first
2. **Add complexity gradually**: Build up to multi-component queries  
3. **Test edge cases**: Typos, partial names, ambiguous phrases
4. **Use batch testing**: Test many variations quickly

## 🚀 Next Steps

1. **Start with interactive mode** to explore
2. **Create your own query file** for your specific use cases
3. **Add custom test cases** for systematic validation
4. **Monitor confidence scores** to identify improvement areas
5. **Iterate and refine** your queries based on results

---

Happy testing! 🏀 The more you test, the better you'll understand how to craft effective natural language queries for NBA data. 