#!/usr/bin/env python3
"""
Confidence Calibration Test Suite

Creates 100 test cases with varying clarity levels to test NLP parser
confidence calibration and determine optimal confidence threshold for LLM fallback.
"""

import os
import sys
import time
import json
import statistics
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass, asdict
from datetime import datetime

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.nl_query.parser import BaseQueryParser, QueryComponents
from sqlalchemy import create_engine
from config import Config


@dataclass
class ConfidenceTestCase:
    """Test case for confidence calibration."""
    
    query: str
    category: str
    clarity_level: str  # "very_clear", "clear", "moderate", "unclear", "very_unclear"
    expected_difficulty: int  # 1-5 scale (1=easy, 5=very hard)
    
    # Expected parsing success (for validation)
    should_parse_successfully: bool = True
    expected_components: Optional[Dict[str, Any]] = None
    notes: str = ""


@dataclass
class ConfidenceTestResult:
    """Result of confidence calibration test."""
    
    test_case: ConfidenceTestCase
    success: bool
    components: Optional[QueryComponents]
    confidence: float
    processing_time: float
    errors: List[str]
    
    # Analysis fields
    confidence_accurate: bool = False  # Does confidence match actual success?
    confidence_category: str = ""  # "high", "medium", "low"


class ConfidenceCalibrationTester:
    """Comprehensive confidence calibration testing."""
    
    def __init__(self):
        """Initialize the confidence tester."""
        self.engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
        self.parser = BaseQueryParser(self.engine)
        print(f"✅ NLP Parser initialized for confidence testing")
    
    def get_comprehensive_test_cases(self) -> List[ConfidenceTestCase]:
        """
        Get 100 comprehensive test cases with varying clarity levels.
        
        Returns:
            List of test cases spanning clear to unclear queries
        """
        test_cases = []
        
        # Very Clear Queries (20 cases) - Should have high confidence
        very_clear = [
            ConfidenceTestCase("LeBron last 10 games", "simple", "very_clear", 1, True, notes="Basic player + time"),
            ConfidenceTestCase("Curry this season with 30+ points", "simple", "very_clear", 1, True, notes="Player + stat filter"),
            ConfidenceTestCase("Giannis home games", "simple", "very_clear", 1, True, notes="Player + location"),
            ConfidenceTestCase("Dame recent 15 games with 8+ threes", "simple", "very_clear", 1, True, notes="Player + time + stat"),
            ConfidenceTestCase("KD away games with 25+ points", "simple", "very_clear", 1, True, notes="Player + location + stat"),
            ConfidenceTestCase("Jokic games where he scores 20+ points", "simple", "very_clear", 1, True, notes="Clear statistical filter"),
            ConfidenceTestCase("Embiid last 5 games at home", "simple", "very_clear", 1, True, notes="Player + time + location"),
            ConfidenceTestCase("Tatum this season", "simple", "very_clear", 1, True, notes="Player + time period"),
            ConfidenceTestCase("Luka recent games with 35+ points", "simple", "very_clear", 1, True, notes="Player + stat filter"),
            ConfidenceTestCase("Harden games with 10+ assists", "simple", "very_clear", 1, True, notes="Player + assists filter"),
            ConfidenceTestCase("AD games with 12+ rebounds", "simple", "very_clear", 1, True, notes="Nickname + rebounds"),
            ConfidenceTestCase("Westbrook triple-double games", "simple", "very_clear", 1, True, notes="Player + stat concept"),
            ConfidenceTestCase("Butler last 20 games", "simple", "very_clear", 1, True, notes="Player + time filter"),
            ConfidenceTestCase("Kawhi games where he plays 30+ minutes", "simple", "very_clear", 1, True, notes="Player + minutes"),
            ConfidenceTestCase("Beal games with 25+ points this season", "simple", "very_clear", 1, True, notes="Player + stat + time"),
            ConfidenceTestCase("CP3 home games with 8+ assists", "simple", "very_clear", 1, True, notes="Nickname + location + stat"),
            ConfidenceTestCase("Zion recent games with double-digit points", "simple", "very_clear", 1, True, notes="Player + stat phrase"),
            ConfidenceTestCase("Ja Morant last 10 games", "simple", "very_clear", 1, True, notes="Full name + time"),
            ConfidenceTestCase("Draymond games with 5+ assists", "simple", "very_clear", 1, True, notes="First name + stat"),
            ConfidenceTestCase("Klay this season with 6+ threes", "simple", "very_clear", 1, True, notes="Nickname + stat"),
        ]
        
        # Clear Queries (20 cases) - Should have good confidence  
        clear = [
            ConfidenceTestCase("LeBron with AD last 10 games", "relationships", "clear", 2, True, notes="Player relationships"),
            ConfidenceTestCase("Curry against top 5 defenses", "opponent", "clear", 2, True, notes="Opponent filter"),
            ConfidenceTestCase("Giannis games where he gets 25+ points and 10+ rebounds", "multi_stat", "clear", 2, True, notes="Multiple stats"),
            ConfidenceTestCase("Dame games vs three point defenses", "opponent", "clear", 2, True, notes="Defensive category"),
            ConfidenceTestCase("Luka home games with Kyrie", "relationships", "clear", 2, True, notes="Location + teammate"),
            ConfidenceTestCase("Tatum with Brown last 15 games where he scores 20+ points", "complex", "clear", 2, True, notes="Relationship + stat"),
            ConfidenceTestCase("Embiid games where he plays between 30 and 35 minutes", "range", "clear", 2, True, notes="Minutes range"),
            ConfidenceTestCase("Harden recent games with double-digit assists", "stat_phrase", "clear", 2, True, notes="Stat description"),
            ConfidenceTestCase("Jokic away games with triple-double", "stat_concept", "clear", 2, True, notes="Statistical concept"),
            ConfidenceTestCase("KD games against elite defenses", "opponent", "clear", 2, True, notes="Defense quality"),
            ConfidenceTestCase("Kawhi games where he shoots 50%+ from field", "advanced_stat", "clear", 2, True, notes="Percentage stat"),
            ConfidenceTestCase("Butler games with 6+ rebounds and 5+ assists", "multi_stat", "clear", 2, True, notes="Multiple categories"),
            ConfidenceTestCase("Westbrook games without triple-double", "negative_stat", "clear", 2, True, notes="Absence of stat"),
            ConfidenceTestCase("Beal home games this season with 30+ points", "complete", "clear", 2, True, notes="Multiple filters"),
            ConfidenceTestCase("Paul George recent 12 games", "full_name", "clear", 2, True, notes="Full name variant"),
            ConfidenceTestCase("Alperen Sengun games with 15+ points", "lesser_known", "clear", 2, True, notes="Less famous player"),
            ConfidenceTestCase("Dejounte Murray games with 8+ assists", "full_name", "clear", 2, True, notes="Full name + stat"),
            ConfidenceTestCase("Julius Randle games with double-digit rebounds", "stat_phrase", "clear", 2, True, notes="Stat description"),
            ConfidenceTestCase("Tyler Herro games where he makes 4+ threes", "clear_stat", "clear", 2, True, notes="Specific stat"),
            ConfidenceTestCase("Scottie Barnes recent games with 12+ points", "rookie", "clear", 2, True, notes="Younger player"),
        ]
        
        # Moderate Clarity (25 cases) - Mixed confidence expected
        moderate = [
            ConfidenceTestCase("LeBron with AD but not Russ last 10 games", "complex_relationship", "moderate", 3, True, notes="With/without logic"),
            ConfidenceTestCase("Curry vs worst defensive teams recent games", "opponent_quality", "moderate", 3, True, notes="Worst teams"),
            ConfidenceTestCase("Giannis when he's aggressive scoring wise", "subjective", "moderate", 3, False, notes="Subjective term"),
            ConfidenceTestCase("Dame in clutch situations with 25+ points", "context", "moderate", 3, False, notes="Clutch context"),
            ConfidenceTestCase("Luka games where he goes off", "colloquial", "moderate", 3, False, notes="Slang expression"),
            ConfidenceTestCase("Tatum efficient shooting games this season", "efficiency", "moderate", 3, False, notes="Vague efficiency"),
            ConfidenceTestCase("Embiid dominant paint games", "descriptive", "moderate", 3, False, notes="Paint dominance"),
            ConfidenceTestCase("Jokic facilitating games with 8+ assists", "style", "moderate", 3, True, notes="Playing style"),
            ConfidenceTestCase("KD when he's feeling it from three", "feeling", "moderate", 3, False, notes="Subjective state"),
            ConfidenceTestCase("Harden step-back three games", "signature_move", "moderate", 3, False, notes="Signature move"),
            ConfidenceTestCase("Kawhi load management games", "context", "moderate", 3, False, notes="Rest context"),
            ConfidenceTestCase("Butler intense playoff-style games", "style", "moderate", 3, False, notes="Game style"),
            ConfidenceTestCase("Westbrook high usage games", "analytics", "moderate", 3, False, notes="Usage rate"),
            ConfidenceTestCase("Beal volume shooting games", "style", "moderate", 3, False, notes="Shot volume"),
            ConfidenceTestCase("CP3 orchestrating games with assists", "style", "moderate", 3, True, notes="Facilitating style"),
            ConfidenceTestCase("Draymond defensive anchor games", "role", "moderate", 3, False, notes="Defensive role"),
            ConfidenceTestCase("Klay hot shooting games from three", "hot_streak", "moderate", 3, False, notes="Hot shooting"),
            ConfidenceTestCase("Zion attacking the rim games", "style", "moderate", 3, False, notes="Playing style"),
            ConfidenceTestCase("Ja explosive games with 25+ points", "descriptive", "moderate", 3, True, notes="Explosive style"),
            ConfidenceTestCase("PG13 two-way games", "nickname", "moderate", 3, False, notes="Two-way play"),
            ConfidenceTestCase("The Greek Freak monster games", "nickname", "moderate", 3, False, notes="Monster games"),
            ConfidenceTestCase("King James vintage performances", "nickname", "moderate", 3, False, notes="Vintage performance"),
            ConfidenceTestCase("Chef Curry cooking from deep", "nickname", "moderate", 3, False, notes="Cooking metaphor"),
            ConfidenceTestCase("Black Mamba mentality games", "deceased_player", "moderate", 3, False, notes="Kobe reference"),
            ConfidenceTestCase("The Slim Reaper efficient games", "nickname", "moderate", 3, False, notes="Efficient games"),
        ]
        
        # Unclear Queries (20 cases) - Should have lower confidence
        unclear = [
            ConfidenceTestCase("LeBron when he's locked in mentally", "mental_state", "unclear", 4, False, notes="Mental state"),
            ConfidenceTestCase("Curry shooting lights out recently", "metaphor", "unclear", 4, False, notes="Shooting metaphor"),
            ConfidenceTestCase("Giannis bulldozing through defenses", "metaphor", "unclear", 4, False, notes="Physical metaphor"),
            ConfidenceTestCase("Dame logo range games", "range", "unclear", 4, False, notes="Deep three reference"),
            ConfidenceTestCase("Luka magic happens games", "magic", "unclear", 4, False, notes="Magic reference"),
            ConfidenceTestCase("Tatum taking over fourth quarter", "takeover", "unclear", 4, False, notes="Fourth quarter takeover"),
            ConfidenceTestCase("Embiid unstoppable in post", "unstoppable", "unclear", 4, False, notes="Unstoppable play"),
            ConfidenceTestCase("Jokic chess master games", "metaphor", "unclear", 4, False, notes="Chess metaphor"),
            ConfidenceTestCase("KD pure scorer mode", "mode", "unclear", 4, False, notes="Scorer mode"),
            ConfidenceTestCase("Harden drawing fouls all game", "style", "unclear", 4, False, notes="Foul drawing"),
            ConfidenceTestCase("Kawhi robotic efficiency games", "metaphor", "unclear", 4, False, notes="Robot metaphor"),
            ConfidenceTestCase("Butler gritty performances", "gritty", "unclear", 4, False, notes="Gritty play"),
            ConfidenceTestCase("Westbrook triple-double hunting", "hunting", "unclear", 4, False, notes="Stat hunting"),
            ConfidenceTestCase("Beal microwave scoring", "microwave", "unclear", 4, False, notes="Microwave scorer"),
            ConfidenceTestCase("CP3 point god games", "nickname", "unclear", 4, False, notes="Point god reference"),
            ConfidenceTestCase("Draymond talking trash defensively", "trash_talk", "unclear", 4, False, notes="Trash talking"),
            ConfidenceTestCase("Klay toaster mode activated", "meme", "unclear", 4, False, notes="Toaster meme"),
            ConfidenceTestCase("Zion freight train drives", "metaphor", "unclear", 4, False, notes="Freight train"),
            ConfidenceTestCase("Ja poster dunk games", "highlight", "unclear", 4, False, notes="Poster dunks"),
            ConfidenceTestCase("AD glass man injury games", "injury", "unclear", 4, False, notes="Injury prone"),
        ]
        
        # Very Unclear/Problematic (15 cases) - Should have very low confidence
        very_unclear = [
            ConfidenceTestCase("LeBron father time games", "abstract", "very_unclear", 5, False, notes="Age reference"),
            ConfidenceTestCase("Curry baby face assassin mode", "nickname", "very_unclear", 5, False, notes="Complex nickname"),
            ConfidenceTestCase("Giannis learning english interviews", "off_court", "very_unclear", 5, False, notes="Off-court content"),
            ConfidenceTestCase("Dame loyalty to Portland games", "loyalty", "very_unclear", 5, False, notes="Team loyalty"),
            ConfidenceTestCase("Luka whining to refs games", "behavior", "very_unclear", 5, False, notes="Referee complaints"),
            ConfidenceTestCase("Tatum Kobe workout influence", "training", "very_unclear", 5, False, notes="Training influence"),
            ConfidenceTestCase("Embiid social media trolling", "social_media", "very_unclear", 5, False, notes="Social media"),
            ConfidenceTestCase("Jokic horse racing passion", "hobby", "very_unclear", 5, False, notes="Personal hobby"),
            ConfidenceTestCase("KD burner account controversy", "controversy", "very_unclear", 5, False, notes="Twitter controversy"),
            ConfidenceTestCase("Harden strip club allegations", "allegations", "very_unclear", 5, False, notes="Off-court allegations"),
            ConfidenceTestCase("Kawhi mysterious injury management", "injury", "very_unclear", 5, False, notes="Injury mystery"),
            ConfidenceTestCase("Butler coffee business games", "business", "very_unclear", 5, False, notes="Coffee business"),
            ConfidenceTestCase("Westbrook fashion choices impact", "fashion", "very_unclear", 5, False, notes="Fashion choices"),
            ConfidenceTestCase("Beal contract extension talks", "contract", "very_unclear", 5, False, notes="Contract talks"),
            ConfidenceTestCase("", "empty", "very_unclear", 5, False, notes="Empty query"),
        ]
        
        # Combine all test cases
        test_cases.extend(very_clear)
        test_cases.extend(clear)  
        test_cases.extend(moderate)
        test_cases.extend(unclear)
        test_cases.extend(very_unclear)
        
        return test_cases
    
    def test_single_case(self, test_case: ConfidenceTestCase) -> ConfidenceTestResult:
        """Test a single confidence calibration case."""
        start_time = time.time()
        errors = []
        
        try:
            # Parse with NLP parser
            components = self.parser.parse(test_case.query)
            processing_time = time.time() - start_time
            
            if components is None:
                return ConfidenceTestResult(
                    test_case=test_case,
                    success=False,
                    components=None,
                    confidence=0.0,
                    processing_time=processing_time,
                    errors=["Parser returned None"]
                )
            
            success = components.player_name is not None  # Basic success criteria
            confidence = components.confidence
            
            # Analyze confidence accuracy
            confidence_accurate = self._assess_confidence_accuracy(success, confidence, test_case)
            confidence_category = self._categorize_confidence(confidence)
            
            return ConfidenceTestResult(
                test_case=test_case,
                success=success,
                components=components,
                confidence=confidence,
                processing_time=processing_time,
                errors=errors,
                confidence_accurate=confidence_accurate,
                confidence_category=confidence_category
            )
            
        except Exception as e:
            processing_time = time.time() - start_time
            return ConfidenceTestResult(
                test_case=test_case,
                success=False,
                components=None,
                confidence=0.0,
                processing_time=processing_time,
                errors=[f"Exception: {str(e)}"],
                confidence_accurate=False,
                confidence_category="error"
            )
    
    def _assess_confidence_accuracy(self, success: bool, confidence: float, test_case: ConfidenceTestCase) -> bool:
        """Assess if confidence accurately reflects parsing success."""
        if test_case.should_parse_successfully:
            # Should succeed - confidence should be high if it did, low if it didn't
            if success and confidence >= 0.7:
                return True  # High confidence + success = accurate
            elif not success and confidence < 0.7:
                return True  # Low confidence + failure = accurate
            else:
                return False  # Confidence doesn't match outcome
        else:
            # Should fail - confidence should be low regardless of outcome
            return confidence < 0.7
    
    def _categorize_confidence(self, confidence: float) -> str:
        """Categorize confidence level."""
        if confidence >= 0.8:
            return "high"
        elif confidence >= 0.6:
            return "medium"
        elif confidence >= 0.3:
            return "low"
        else:
            return "very_low"
    
    def analyze_results(self, results: List[ConfidenceTestResult]) -> Dict[str, Any]:
        """Analyze confidence calibration results."""
        total_cases = len(results)
        
        # Overall statistics
        successful_parses = len([r for r in results if r.success])
        accurate_confidences = len([r for r in results if r.confidence_accurate])
        
        # Confidence distribution
        confidence_dist = {
            "high": len([r for r in results if r.confidence_category == "high"]),
            "medium": len([r for r in results if r.confidence_category == "medium"]),
            "low": len([r for r in results if r.confidence_category == "low"]),
            "very_low": len([r for r in results if r.confidence_category == "very_low"])
        }
        
        # Success rate by confidence category
        success_by_confidence = {}
        for category in ["high", "medium", "low", "very_low"]:
            category_results = [r for r in results if r.confidence_category == category]
            if category_results:
                success_rate = len([r for r in category_results if r.success]) / len(category_results)
                success_by_confidence[category] = success_rate
            else:
                success_by_confidence[category] = 0.0
        
        # Clarity level analysis
        clarity_analysis = {}
        for clarity in ["very_clear", "clear", "moderate", "unclear", "very_unclear"]:
            clarity_results = [r for r in results if r.test_case.clarity_level == clarity]
            if clarity_results:
                avg_confidence = statistics.mean([r.confidence for r in clarity_results])
                success_rate = len([r for r in clarity_results if r.success]) / len(clarity_results)
                accuracy_rate = len([r for r in clarity_results if r.confidence_accurate]) / len(clarity_results)
                
                clarity_analysis[clarity] = {
                    "count": len(clarity_results),
                    "avg_confidence": avg_confidence,
                    "success_rate": success_rate,
                    "confidence_accuracy": accuracy_rate
                }
        
        # Determine optimal confidence threshold
        thresholds = [0.5, 0.6, 0.7, 0.8, 0.9]
        threshold_analysis = {}
        
        for threshold in thresholds:
            high_conf_results = [r for r in results if r.confidence >= threshold]
            low_conf_results = [r for r in results if r.confidence < threshold]
            
            high_conf_success = len([r for r in high_conf_results if r.success]) / len(high_conf_results) if high_conf_results else 0
            low_conf_success = len([r for r in low_conf_results if r.success]) / len(low_conf_results) if low_conf_results else 0
            
            threshold_analysis[threshold] = {
                "high_conf_count": len(high_conf_results),
                "low_conf_count": len(low_conf_results),
                "high_conf_success_rate": high_conf_success,
                "low_conf_success_rate": low_conf_success,
                "separation": high_conf_success - low_conf_success
            }
        
        return {
            "summary": {
                "total_cases": total_cases,
                "successful_parses": successful_parses,
                "parse_success_rate": successful_parses / total_cases,
                "accurate_confidences": accurate_confidences,
                "confidence_accuracy_rate": accurate_confidences / total_cases,
                "avg_confidence": statistics.mean([r.confidence for r in results]),
                "avg_processing_time": statistics.mean([r.processing_time for r in results])
            },
            "confidence_distribution": confidence_dist,
            "success_by_confidence": success_by_confidence,
            "clarity_analysis": clarity_analysis,
            "threshold_analysis": threshold_analysis
        }
    
    def print_detailed_results(self, results: List[ConfidenceTestResult], analysis: Dict[str, Any]) -> None:
        """Print detailed confidence calibration results."""
        print("\n" + "="*100)
        print("🎯 CONFIDENCE CALIBRATION TEST RESULTS")
        print("="*100)
        
        # Overall Summary
        summary = analysis["summary"]
        print(f"\n📊 OVERALL PERFORMANCE:")
        print(f"   Total Test Cases: {summary['total_cases']}")
        print(f"   Parse Success Rate: {summary['parse_success_rate']:.1%} ({summary['successful_parses']}/{summary['total_cases']})")
        print(f"   Confidence Accuracy: {summary['confidence_accuracy_rate']:.1%} ({summary['accurate_confidences']}/{summary['total_cases']})")
        print(f"   Average Confidence: {summary['avg_confidence']:.3f}")
        print(f"   Average Processing Time: {summary['avg_processing_time']:.3f}s")
        
        # Confidence Distribution
        print(f"\n📈 CONFIDENCE DISTRIBUTION:")
        conf_dist = analysis["confidence_distribution"]
        for category, count in conf_dist.items():
            percentage = count / summary['total_cases'] * 100
            print(f"   {category.title():<10} {count:>3} cases ({percentage:>5.1f}%)")
        
        # Success Rate by Confidence
        print(f"\n🎯 SUCCESS RATE BY CONFIDENCE LEVEL:")
        success_by_conf = analysis["success_by_confidence"]
        for category, rate in success_by_conf.items():
            print(f"   {category.title():<10} {rate:.1%} success rate")
        
        # Clarity Level Analysis
        print(f"\n🔍 ANALYSIS BY CLARITY LEVEL:")
        clarity_analysis = analysis["clarity_analysis"]
        for clarity, stats in clarity_analysis.items():
            print(f"   {clarity.replace('_', ' ').title():<12} "
                  f"Avg Conf: {stats['avg_confidence']:.3f} | "
                  f"Success: {stats['success_rate']:.1%} | "
                  f"Accuracy: {stats['confidence_accuracy']:.1%} "
                  f"({stats['count']} cases)")
        
        # Threshold Analysis
        print(f"\n⚖️  CONFIDENCE THRESHOLD ANALYSIS:")
        threshold_analysis = analysis["threshold_analysis"]
        print(f"   {'Threshold':<10} {'High Conf':<10} {'Low Conf':<9} {'High Success':<12} {'Low Success':<11} {'Separation'}")
        print(f"   {'-'*10} {'-'*10} {'-'*9} {'-'*12} {'-'*11} {'-'*10}")
        
        best_threshold = 0.7
        best_separation = 0.0
        
        for threshold, stats in threshold_analysis.items():
            separation = stats['separation']
            if separation > best_separation:
                best_separation = separation
                best_threshold = threshold
            
            print(f"   {threshold:<10.1f} {stats['high_conf_count']:<10} {stats['low_conf_count']:<9} "
                  f"{stats['high_conf_success_rate']:<12.1%} {stats['low_conf_success_rate']:<11.1%} "
                  f"{separation:<10.3f}")
        
        print(f"\n🎯 RECOMMENDED CONFIDENCE THRESHOLD: {best_threshold}")
        print(f"   This threshold provides {best_separation:.3f} separation between high/low confidence success rates")
        
        # Failed Cases Analysis
        failed_cases = [r for r in results if not r.success and r.test_case.should_parse_successfully]
        if failed_cases:
            print(f"\n❌ UNEXPECTED FAILURES ({len(failed_cases)} cases):")
            for i, failure in enumerate(failed_cases[:5], 1):  # Show first 5
                print(f"   {i}. [{failure.test_case.clarity_level}] {failure.test_case.query}")
                print(f"      Confidence: {failure.confidence:.3f} | Expected: Success")
        
        # Confidence Mismatches
        mismatches = [r for r in results if not r.confidence_accurate]
        if mismatches:
            print(f"\n⚠️  CONFIDENCE MISMATCHES ({len(mismatches)} cases):")
            for i, mismatch in enumerate(mismatches[:5], 1):  # Show first 5
                result_str = "Success" if mismatch.success else "Failed"
                expected_str = "Success" if mismatch.test_case.should_parse_successfully else "Failure"
                print(f"   {i}. [{mismatch.test_case.clarity_level}] {mismatch.test_case.query[:60]}...")
                print(f"      Confidence: {mismatch.confidence:.3f} | Result: {result_str} | Expected: {expected_str}")
        
        print("\n" + "="*100)
    
    def run_confidence_test(self) -> None:
        """Run comprehensive confidence calibration test."""
        print("🚀 Starting Confidence Calibration Test...")
        
        test_cases = self.get_comprehensive_test_cases()
        print(f"📝 Testing {len(test_cases)} cases across clarity levels...")
        
        all_results = []
        
        for i, test_case in enumerate(test_cases, 1):
            print(f"   [{i}/{len(test_cases)}] {test_case.clarity_level}: {test_case.query[:50]}...")
            
            result = self.test_single_case(test_case)
            all_results.append(result)
            
            # Brief status
            status = "✅" if result.success else "❌"
            conf_status = "🎯" if result.confidence_accurate else "⚠️"
            print(f"      {status} {conf_status} Conf: {result.confidence:.3f}")
        
        # Analyze and print results
        print(f"\n📊 Analyzing {len(all_results)} test results...")
        analysis = self.analyze_results(all_results)
        self.print_detailed_results(all_results, analysis)
        
        # Save detailed results
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_file = f"confidence_calibration_results_{timestamp}.json"
        
        output_data = {
            "timestamp": timestamp,
            "test_summary": analysis,
            "detailed_results": [asdict(result) for result in all_results]
        }
        
        with open(results_file, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        
        print(f"\n💾 Detailed results saved to: {results_file}")


def main():
    """Main function to run confidence calibration testing."""
    try:
        tester = ConfidenceCalibrationTester()
        tester.run_confidence_test()
        
    except Exception as e:
        print(f"❌ Error during confidence testing: {e}")


if __name__ == "__main__":
    main() 