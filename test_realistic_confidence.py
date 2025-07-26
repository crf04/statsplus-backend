#!/usr/bin/env python3
"""
Realistic Confidence Calibration Test Suite

Creates 100 realistic test cases that reflect how actual NBA fans 
with background knowledge would phrase their queries.
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
class RealisticTestCase:
    """Realistic test case for confidence calibration."""
    
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
    
    test_case: RealisticTestCase
    success: bool
    components: Optional[QueryComponents]
    confidence: float
    processing_time: float
    errors: List[str]
    
    # Analysis fields
    confidence_accurate: bool = False  # Does confidence match actual success?
    confidence_category: str = ""  # "high", "medium", "low"


class RealisticConfidenceTester:
    """Realistic confidence calibration testing."""
    
    def __init__(self):
        """Initialize the confidence tester."""
        self.engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
        self.parser = BaseQueryParser(self.engine)
        print(f"✅ NLP Parser initialized for realistic confidence testing")
    
    def get_realistic_test_cases(self) -> List[RealisticTestCase]:
        """
        Get 100 realistic test cases that actual NBA fans would ask.
        
        Returns:
            List of realistic test cases with varying clarity levels
        """
        test_cases = []
        
        # Very Clear Queries (25 cases) - Perfect syntax, common requests
        very_clear = [
            RealisticTestCase("LeBron last 10 games", "basic", "very_clear", 1, True, notes="Standard player + time"),
            RealisticTestCase("Curry this season with 30+ points", "stat_filter", "very_clear", 1, True, notes="Player + stat filter"),
            RealisticTestCase("Giannis home games", "location", "very_clear", 1, True, notes="Player + location"),
            RealisticTestCase("Dame last 15 games with 8+ threes", "stat_filter", "very_clear", 1, True, notes="Player + time + stat"),
            RealisticTestCase("KD away games with 25+ points", "stat_location", "very_clear", 1, True, notes="Player + location + stat"),
            RealisticTestCase("Jokic games with 20+ points", "stat_filter", "very_clear", 1, True, notes="Clear statistical filter"),
            RealisticTestCase("Embiid last 5 games at home", "location_time", "very_clear", 1, True, notes="Player + time + location"),
            RealisticTestCase("Tatum this season", "time_period", "very_clear", 1, True, notes="Player + time period"),
            RealisticTestCase("Luka recent games with 35+ points", "stat_filter", "very_clear", 1, True, notes="Player + stat filter"),
            RealisticTestCase("Harden games with 10+ assists", "stat_filter", "very_clear", 1, True, notes="Player + assists filter"),
            RealisticTestCase("Anthony Davis games with 12+ rebounds", "stat_filter", "very_clear", 1, True, notes="Full name + rebounds"),
            RealisticTestCase("Westbrook triple-double games", "stat_concept", "very_clear", 1, True, notes="Player + stat concept"),
            RealisticTestCase("Jimmy Butler last 20 games", "basic", "very_clear", 1, True, notes="Full name + time filter"),
            RealisticTestCase("Kawhi games with 30+ minutes", "minutes", "very_clear", 1, True, notes="Player + minutes filter"),
            RealisticTestCase("Bradley Beal games with 25+ points this season", "complete", "very_clear", 1, True, notes="Complete query"),
            RealisticTestCase("Chris Paul home games with 8+ assists", "complete", "very_clear", 1, True, notes="Full name + location + stat"),
            RealisticTestCase("Zion games with double-digit points", "stat_phrase", "very_clear", 1, True, notes="Player + stat phrase"),
            RealisticTestCase("Ja Morant last 10 games", "basic", "very_clear", 1, True, notes="Full name + time"),
            RealisticTestCase("Draymond Green games with 5+ assists", "stat_filter", "very_clear", 1, True, notes="Full name + stat"),
            RealisticTestCase("Klay Thompson this season with 6+ threes", "complete", "very_clear", 1, True, notes="Full name + stat"),
            RealisticTestCase("Paul George recent 12 games", "basic", "very_clear", 1, True, notes="Full name + time"),
            RealisticTestCase("Tyler Herro games with 4+ threes", "stat_filter", "very_clear", 1, True, notes="Player + threes"),
            RealisticTestCase("Scottie Barnes games with 12+ points", "stat_filter", "very_clear", 1, True, notes="Player + points"),
            RealisticTestCase("Alperen Sengun games with 15+ points", "stat_filter", "very_clear", 1, True, notes="International player"),
            RealisticTestCase("Dejounte Murray games with 8+ assists", "stat_filter", "very_clear", 1, True, notes="Player + assists"),
        ]
        
        # Clear Queries (25 cases) - Good queries with minor variations
        clear = [
            RealisticTestCase("LeBron with Anthony Davis last 10 games", "relationships", "clear", 2, True, notes="Player relationships"),
            RealisticTestCase("Curry against top 5 defenses", "opponent", "clear", 2, True, notes="Opponent filter"),
            RealisticTestCase("Giannis games with 25+ points and 10+ rebounds", "multi_stat", "clear", 2, True, notes="Multiple stats"),
            RealisticTestCase("Dame vs top 10 three point defenses", "opponent", "clear", 2, True, notes="Specific defense type"),
            RealisticTestCase("Luka home games with Kyrie Irving", "relationships", "clear", 2, True, notes="Location + teammate"),
            RealisticTestCase("Tatum with Jaylen Brown last 15 games scoring 20+", "complex", "clear", 2, True, notes="Complex relationship query"),
            RealisticTestCase("Embiid games playing between 30 and 35 minutes", "range", "clear", 2, True, notes="Minutes range"),
            RealisticTestCase("Harden recent games with double-digit assists", "stat_phrase", "clear", 2, True, notes="Stat description"),
            RealisticTestCase("Jokic away games with triple-double", "stat_concept", "clear", 2, True, notes="Away + triple-double"),
            RealisticTestCase("KD games against elite defensive teams", "opponent", "clear", 2, True, notes="Elite defense"),
            RealisticTestCase("Kawhi games shooting 50%+ from the field", "advanced_stat", "clear", 2, True, notes="Shooting percentage"),
            RealisticTestCase("Butler games with 6+ rebounds and 5+ assists", "multi_stat", "clear", 2, True, notes="Multiple categories"),
            RealisticTestCase("Westbrook games without a triple-double", "negative_stat", "clear", 2, True, notes="Without stat"),
            RealisticTestCase("Beal home games this season scoring 30+", "complete", "clear", 2, True, notes="Home + season + stat"),
            RealisticTestCase("CP3 games with Russell Westbrook", "relationships", "clear", 2, True, notes="Teammate relationship"),
            RealisticTestCase("Donovan Mitchell games with 25+ points", "stat_filter", "clear", 2, True, notes="Full name + stat"),
            RealisticTestCase("Rudy Gobert games with 10+ rebounds", "stat_filter", "clear", 2, True, notes="Center + rebounds"),
            RealisticTestCase("Devin Booker away games with 30+ points", "stat_location", "clear", 2, True, notes="Away + scoring"),
            RealisticTestCase("Karl-Anthony Towns games with double-double", "stat_concept", "clear", 2, True, notes="Hyphenated name"),
            RealisticTestCase("De'Aaron Fox games with 8+ assists", "stat_filter", "clear", 2, True, notes="Apostrophe in name"),
            RealisticTestCase("Nikola Vucevic games with 15+ rebounds", "stat_filter", "clear", 2, True, notes="International name"),
            RealisticTestCase("CJ McCollum games with 5+ threes", "stat_filter", "clear", 2, True, notes="Initials + last name"),
            RealisticTestCase("Mikal Bridges games playing 35+ minutes", "minutes", "clear", 2, True, notes="Minutes filter"),
            RealisticTestCase("Fred VanVleet games with 6+ assists", "stat_filter", "clear", 2, True, notes="Compound last name"),
            RealisticTestCase("OG Anunoby games with 15+ points", "stat_filter", "clear", 2, True, notes="Unusual name"),
        ]
        
        # Moderate Clarity (25 cases) - Realistic but somewhat informal/ambiguous
        moderate = [
            RealisticTestCase("LeBron with AD but without Westbrook last 10", "complex_relationship", "moderate", 3, True, notes="With/without logic"),
            RealisticTestCase("Curry vs worst defensive teams recently", "opponent_time", "moderate", 3, True, notes="Worst + recent"),
            RealisticTestCase("Giannis efficient games this season", "efficiency", "moderate", 3, False, notes="Vague efficiency"),
            RealisticTestCase("Dame clutch games with 25+ points", "context", "moderate", 3, False, notes="Clutch context"),
            RealisticTestCase("Luka big games recently", "subjective", "moderate", 3, False, notes="Vague 'big games'"),
            RealisticTestCase("Tatum good shooting games this season", "efficiency", "moderate", 3, False, notes="Vague 'good shooting'"),
            RealisticTestCase("Embiid dominant games in the paint", "style", "moderate", 3, False, notes="Subjective dominance"),
            RealisticTestCase("Jokic playmaking games with 8+ assists", "style", "moderate", 3, True, notes="Playmaking style"),
            RealisticTestCase("KD hot shooting games from three", "streaks", "moderate", 3, False, notes="Hot shooting streak"),
            RealisticTestCase("Harden high scoring games", "subjective", "moderate", 3, False, notes="Vague high scoring"),
            RealisticTestCase("Kawhi healthy games this season", "health", "moderate", 3, False, notes="Health status"),
            RealisticTestCase("Butler intense games", "subjective", "moderate", 3, False, notes="Subjective intensity"),
            RealisticTestCase("Westbrook explosive games", "subjective", "moderate", 3, False, notes="Subjective explosive"),
            RealisticTestCase("Beal volume scoring games", "style", "moderate", 3, False, notes="Volume scoring"),
            RealisticTestCase("Draymond impactful games defensively", "style", "moderate", 3, False, notes="Defensive impact"),
            RealisticTestCase("Klay hot games from three point line", "streaks", "moderate", 3, False, notes="Three point hot streak"),
            RealisticTestCase("Zion attacking games", "style", "moderate", 3, False, notes="Attacking style"),
            RealisticTestCase("Ja explosive scoring games", "style", "moderate", 3, False, notes="Explosive scoring"),
            RealisticTestCase("Morant highlight games", "subjective", "moderate", 3, False, notes="Highlight performances"),
            RealisticTestCase("Greek Freak monster games", "nickname", "moderate", 3, False, notes="Nickname + monster"),
            RealisticTestCase("King James vintage games", "nickname", "moderate", 3, False, notes="Vintage performances"),
            RealisticTestCase("Chef Curry three point games", "nickname", "moderate", 3, False, notes="Chef nickname"),
            RealisticTestCase("The Process dominant games", "nickname", "moderate", 3, False, notes="Embiid nickname"),
            RealisticTestCase("Dame Time clutch performances", "nickname", "moderate", 3, False, notes="Dame Time reference"),
            RealisticTestCase("Air Jordan level games", "comparison", "moderate", 3, False, notes="Jordan comparison"),
        ]
        
        # Unclear Queries (15 cases) - Realistic but poorly structured
        unclear = [
            RealisticTestCase("LeBron when he goes off", "vague", "unclear", 4, False, notes="Vague 'goes off'"),
            RealisticTestCase("Curry from way downtown", "location_vague", "unclear", 4, False, notes="Way downtown reference"),
            RealisticTestCase("Giannis unstoppable games", "subjective", "unclear", 4, False, notes="Unstoppable descriptor"),
            RealisticTestCase("Dame deep range games", "range_vague", "unclear", 4, False, notes="Deep range shooting"),
            RealisticTestCase("Luka doing Luka things", "meme", "unclear", 4, False, notes="Doing X things meme"),
            RealisticTestCase("Tatum iso games", "abbreviated", "unclear", 4, False, notes="Isolation abbreviated"),
            RealisticTestCase("Embiid in the zone", "subjective", "unclear", 4, False, notes="In the zone reference"),
            RealisticTestCase("Jokic magic games", "subjective", "unclear", 4, False, notes="Magic performances"),
            RealisticTestCase("KD being KD", "meme", "unclear", 4, False, notes="Being X meme"),
            RealisticTestCase("Harden doing his thing", "vague", "unclear", 4, False, notes="Doing his thing"),
            RealisticTestCase("Kawhi when healthy", "conditional", "unclear", 4, False, notes="Health conditional"),
            RealisticTestCase("Butler locked in", "subjective", "unclear", 4, False, notes="Locked in mentality"),
            RealisticTestCase("Westbrook going crazy", "slang", "unclear", 4, False, notes="Going crazy slang"),
            RealisticTestCase("Beal on fire", "metaphor", "unclear", 4, False, notes="On fire metaphor"),
            RealisticTestCase("Paul George feeling it", "subjective", "unclear", 4, False, notes="Feeling it reference"),
        ]
        
        # Very Unclear/Edge Cases (10 cases) - Typos, incomplete, but still realistic
        very_unclear = [
            RealisticTestCase("lebron last games", "typo", "very_unclear", 5, True, notes="Lowercase name"),
            RealisticTestCase("curry 3s", "abbreviated", "very_unclear", 5, False, notes="Very abbreviated"),
            RealisticTestCase("giannas home", "typo", "very_unclear", 5, False, notes="Misspelled name"),
            RealisticTestCase("dame threes recent", "word_order", "very_unclear", 5, True, notes="Unusual word order"),
            RealisticTestCase("luka last", "incomplete", "very_unclear", 5, False, notes="Incomplete query"),
            RealisticTestCase("tatum pts", "abbreviated", "very_unclear", 5, False, notes="Abbreviated stats"),
            RealisticTestCase("embid games", "typo", "very_unclear", 5, False, notes="Misspelled name"),
            RealisticTestCase("jokic tripledouble", "spacing", "very_unclear", 5, True, notes="No spacing"),
            RealisticTestCase("kd points home games", "word_order", "very_unclear", 5, True, notes="Mixed word order"),
            RealisticTestCase("", "empty", "very_unclear", 5, False, notes="Empty query"),
        ]
        
        # Combine all test cases
        test_cases.extend(very_clear)
        test_cases.extend(clear)  
        test_cases.extend(moderate)
        test_cases.extend(unclear)
        test_cases.extend(very_unclear)
        
        return test_cases
    
    def test_single_case(self, test_case: RealisticTestCase) -> ConfidenceTestResult:
        """Test a single realistic confidence calibration case."""
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
    
    def _assess_confidence_accuracy(self, success: bool, confidence: float, test_case: RealisticTestCase) -> bool:
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
        """Analyze realistic confidence calibration results."""
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
        thresholds = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9]
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
        """Print detailed realistic confidence calibration results."""
        print("\n" + "="*100)
        print("🎯 REALISTIC CONFIDENCE CALIBRATION TEST RESULTS")
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
            if conf_dist[category] > 0:  # Only show categories with cases
                print(f"   {category.title():<10} {rate:.1%} success rate ({conf_dist[category]} cases)")
        
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
            if separation > best_separation and stats['low_conf_count'] > 0:
                best_separation = separation
                best_threshold = threshold
            
            print(f"   {threshold:<10.2f} {stats['high_conf_count']:<10} {stats['low_conf_count']:<9} "
                  f"{stats['high_conf_success_rate']:<12.1%} {stats['low_conf_success_rate']:<11.1%} "
                  f"{separation:<10.3f}")
        
        print(f"\n🎯 RECOMMENDED CONFIDENCE THRESHOLD: {best_threshold}")
        print(f"   This threshold provides {best_separation:.3f} separation between high/low confidence success rates")
        
        # Failed Cases Analysis
        failed_cases = [r for r in results if not r.success and r.test_case.should_parse_successfully]
        if failed_cases:
            print(f"\n❌ UNEXPECTED FAILURES ({len(failed_cases)} cases):")
            for i, failure in enumerate(failed_cases[:8], 1):  # Show first 8
                print(f"   {i}. [{failure.test_case.clarity_level}] '{failure.test_case.query}'")
                print(f"      Confidence: {failure.confidence:.3f} | Notes: {failure.test_case.notes}")
        
        # Success on Should-Fail Cases
        should_fail_success = [r for r in results if r.success and not r.test_case.should_parse_successfully]
        if should_fail_success:
            print(f"\n⚠️  UNEXPECTED SUCCESSES ({len(should_fail_success)} cases):")
            for i, success in enumerate(should_fail_success[:5], 1):  # Show first 5
                print(f"   {i}. [{success.test_case.clarity_level}] '{success.test_case.query}'")
                print(f"      Confidence: {success.confidence:.3f} | Notes: {success.test_case.notes}")
        
        print("\n" + "="*100)
    
    def run_realistic_test(self) -> None:
        """Run realistic confidence calibration test."""
        print("🚀 Starting Realistic Confidence Calibration Test...")
        print("   Testing realistic queries that actual NBA fans would ask")
        
        test_cases = self.get_realistic_test_cases()
        print(f"📝 Testing {len(test_cases)} realistic cases across clarity levels...")
        
        all_results = []
        
        for i, test_case in enumerate(test_cases, 1):
            print(f"   [{i}/{len(test_cases)}] {test_case.clarity_level}: '{test_case.query}'")
            
            result = self.test_single_case(test_case)
            all_results.append(result)
            
            # Brief status
            status = "✅" if result.success else "❌"
            conf_status = "🎯" if result.confidence_accurate else "⚠️"
            expected = "✓" if test_case.should_parse_successfully else "✗"
            print(f"      {status} {conf_status} Conf: {result.confidence:.3f} (Expected: {expected})")
        
        # Analyze and print results
        print(f"\n📊 Analyzing {len(all_results)} realistic test results...")
        analysis = self.analyze_results(all_results)
        self.print_detailed_results(all_results, analysis)
        
        # Save detailed results
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_file = f"realistic_confidence_results_{timestamp}.json"
        
        output_data = {
            "timestamp": timestamp,
            "test_summary": analysis,
            "detailed_results": [asdict(result) for result in all_results]
        }
        
        with open(results_file, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False, default=str)
        
        print(f"\n💾 Detailed results saved to: {results_file}")


def main():
    """Main function to run realistic confidence calibration testing."""
    try:
        tester = RealisticConfidenceTester()
        tester.run_realistic_test()
        
    except Exception as e:
        print(f"❌ Error during realistic confidence testing: {e}")


if __name__ == "__main__":
    main() 