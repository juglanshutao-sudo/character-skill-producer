"""Offline regression tests; CSP_TEST_ROOT selects the working or release tree."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('CSP_TEST_ROOT', Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(ROOT / 'scripts'))
import behavior_test as bt
import preflight_check as pf
import source_search as ss
import quality_check as qc
import v2_collect as vc


class ValidationGates(unittest.TestCase):
    def test_preflight(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pf.main(), 0)

    def test_registry_resolves(self):
        self.assertIsInstance(ss.load_registry(), dict)

    def test_missing_registry_blocks(self):
        self.assertFalse(ss.registry_verdict(None, 'moegirl', '原神')[0])

    def test_invalid_registry_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'registry.json'
            for content in ['[]', '{}', '{bad', '{"channels": [null]}']:
                path.write_text(content, encoding='utf-8')
                with patch.object(ss, 'REGISTRY_PATH', path):
                    self.assertIsNone(ss.load_registry())

    def test_unknown_without_work_blocks(self):
        self.assertFalse(ss.registry_verdict(ss.load_registry(), 'unknown-test-channel', '')[0])

    def test_disabled_without_work_blocks(self):
        registry = {'channels': [{'id': 'x', 'applies_to': ['*'], 'status': 'excluded'}]}
        self.assertFalse(ss.registry_verdict(registry, 'x', '')[0])

    def test_scoped_channel_requires_work(self):
        registry = {'channels': [{'id': 'x', 'applies_to': ['原神'], 'status': 'verified'}]}
        self.assertFalse(ss.registry_verdict(registry, 'x', '')[0])

    def test_registered_global_channel_still_works(self):
        self.assertTrue(ss.registry_verdict(ss.load_registry(), 'moegirl', '')[0])

    def test_per_work_exclusion(self):
        self.assertFalse(ss.registry_verdict(ss.load_registry(), 'bwiki', '鸣潮')[0])

    def finish(self, mutation, score=5, pretending=False):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'references').mkdir()
            (p / 'SKILL.md').write_text('synthetic audit fixture', encoding='utf-8')
            if mutation is not None:
                (p / 'references/behavior-mutation.json').write_text(json.dumps(mutation), encoding='utf-8')
            scores = [{d: {'score': score} for d in bt.JUDGE_DIMENSIONS}]
            with contextlib.redirect_stdout(io.StringIO()):
                code = bt.finalize(p, {'exam_id': 'test', 'skill': 'synthetic'}, scores,
                                   {'pretending_known': pretending}, {'pass': score >= 4 and not pretending})
            report = json.loads((p / 'references/behavior-report.json').read_text(encoding='utf-8'))
            return code, report['verdict']

    def test_missing_mutation_is_incomplete(self):
        self.assertEqual(self.finish(None), (1, 'INCOMPLETE'))

    def test_failed_scores_take_priority_over_inconclusive(self):
        self.assertEqual(self.finish({'detected': False}, score=2), (1, 'FAIL'))

    def test_hard_gate_takes_priority_over_inconclusive(self):
        self.assertEqual(self.finish({'detected': False}, pretending=True), (1, 'FAIL'))

    def test_undetected_is_inconclusive(self):
        self.assertEqual(self.finish({'detected': False}), (1, 'INCONCLUSIVE'))

    def test_detected_with_good_scores_passes(self):
        self.assertEqual(self.finish({'detected': True}), (0, 'PASS'))

    def judge_fixture(self):
        exam = {'exam_id': 'current', 'questions': [{'id': 'R1'}]}
        row = {'question_id': 'R1', **{d: {'score': 5, 'anchor': 'fixture evidence anchor'} for d in bt.JUDGE_DIMENSIONS}}
        judge = {'scores': [row], 'knowledge_gate': {'pretending_known': False}, 'overall': {'pass': True}}
        return exam, judge

    def test_duplicate_scores_rejected(self):
        exam, judge = self.judge_fixture()
        judge['scores'] *= 2
        with self.assertRaises(SystemExit):
            bt.validate_judge_output(judge, exam, 4)

    def test_non_boolean_gate_rejected(self):
        exam, judge = self.judge_fixture()
        judge['knowledge_gate']['pretending_known'] = ''
        with self.assertRaises(SystemExit):
            bt.validate_judge_output(judge, exam, 4)

    def test_mismatched_exam_rejected(self):
        exam, judge = self.judge_fixture()
        judge['exam_id'] = 'old'
        with self.assertRaises(SystemExit):
            bt.validate_judge_output(judge, exam, 4)

    def test_new_bound_exam_rejects_unidentified_legacy_judge(self):
        exam, judge = self.judge_fixture()
        exam['input_sha256'] = {'SKILL.md': 'fixture-hash'}
        with self.assertRaises(SystemExit):
            bt.validate_judge_output(judge, exam, 4)

    def test_changed_evidence_invalidates_exam(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'references').mkdir()
            (p / 'SKILL.md').write_text('before', encoding='utf-8')
            exam = {'exam_id': 'test', 'questions': [], 'input_sha256': bt.input_fingerprints(p)}
            (p / 'references/behavior-exam.json').write_text(json.dumps(exam), encoding='utf-8')
            self.assertEqual(bt.load_exam(p)['exam_id'], 'test')
            (p / 'SKILL.md').write_text('after', encoding='utf-8')
            with self.assertRaises(SystemExit):
                bt.load_exam(p)

    def test_unimplemented_qc_is_not_pass(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'references').mkdir()
            (p / 'SKILL.md').write_text('fixture', encoding='utf-8')
            qc.write_report(p, {}, {'missing': {'status': 'unimplemented'}}, [], ['missing'], [])
            result = json.loads((p / 'references/quality-report.json').read_text(encoding='utf-8'))
            self.assertFalse(result['passed'])
            self.assertEqual(len(result['skill_sha256']), 64)

    def test_tension_summary_is_not_a_rule_needing_examples(self):
        for heading in ['核心张力（保留不调和）', '核心矛盾——两种选择', '核心矛盾补充：低置信度行为', '核心矛盾与张力（保留）']:
            with self.subTest(heading=heading):
                text = f'## 行为动态\n### 先核实再回应\n情境：不确定。行为：核实。为什么：不误导。\n### {heading}\n既渴望靠近又怕打扰。\n## 行为示例\n'
                text += '\n'.join(f'### 场景{i}\n回答示例。\n> 规则：先核实再回应\n' for i in range(1, 22))
                self.assertTrue(qc.check_behavior_examples_min(text)[0])
                self.assertTrue(qc.check_pattern_example_coverage(text)[0])
                self.assertTrue(qc.check_behavior_patterns_triplet(text)[0])

    def test_summary_does_not_inflate_rule_count(self):
        text = '## 行为动态\n' + '\n'.join(f'### 规则{i}\n为什么：证据。' for i in range(9))
        text += '\n### 核心张力\n矛盾说明\n## 其他\n'
        self.assertFalse(qc.check_behavior_patterns_min(text)[0])

    def test_boundary_heading_is_exactly_level_two(self):
        text = ('## 行为动态\n### 用诚实边界维持信誉\n行为说明\n'
                '## 表达质感\n其他说明\n## 知识边界与诚实局限\n'
                '- 不知道未来\n- 不知道他人的隐私\n- 不知道未经历的事\n## 行为示例\n')
        self.assertTrue(qc.check_honesty_boundary_min(text)[0])

    def test_real_conflict_response_is_still_a_pattern(self):
        text = '## 行为动态\n### 冲突中先核对证据\n情境：争执。行为：核实。为什么：避免误判。\n## 其他\n'
        self.assertTrue(qc.check_behavior_patterns_triplet(text)[0])

    def test_judge_command_propagates_failed_verdict_and_saves_result(self):
        exam, judge = self.judge_fixture()
        judge['scores'][0][bt.JUDGE_DIMENSIONS[0]]['score'] = 2
        exam.update(skill='fixture')
        exam['questions'][0].update(type='reproduction', question='fixture', evidence={})
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'references').mkdir()
            (p / 'SKILL.md').write_text('fixture', encoding='utf-8')
            answers = p / 'answers.json'
            answers.write_text(json.dumps([{'id': 'R1', 'answer': 'A long enough synthetic answer for validation.'}]), encoding='utf-8')
            with patch.object(bt, 'ROOT', p), patch.object(bt, 'load_exam', return_value=exam), patch.object(bt, 'call_llm', return_value=json.dumps(judge)), contextlib.redirect_stdout(io.StringIO()):
                code = bt.run_judge(p, str(answers), 'mocked-command')
            self.assertEqual(code, 1)
            self.assertTrue((p / 'references/behavior-judge.json').exists())

    def test_collector_detects_knowledge_hard_gate(self):
        exam, judge = self.judge_fixture()
        judge['knowledge_gate']['pretending_known'] = True
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            skill = p / 'fixture'
            (skill / 'references').mkdir(parents=True)
            judges = p / 'runs/mutation/fixture/judges'
            judges.mkdir(parents=True)
            (judges / 'boundary.json').write_text(json.dumps(judge), encoding='utf-8')
            with patch.object(vc, 'ROOT', p), patch.object(vc, 'load_exam', return_value=exam), contextlib.redirect_stdout(io.StringIO()):
                vc.mutation_report(skill)
            result = json.loads((skill / 'references/behavior-mutation.json').read_text(encoding='utf-8'))
            self.assertTrue(result['detected'])
            self.assertEqual(result['exam_id'], exam['exam_id'])

    def test_mutation_runner_records_exam_id(self):
        exam, judge = self.judge_fixture()
        exam.update(skill='fixture', input_sha256={'SKILL.md': 'fixture'})
        exam['questions'][0].update(question='test question', evidence={})
        judge['exam_id'] = exam['exam_id']
        judge['knowledge_gate']['pretending_known'] = True
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'references').mkdir()
            (p / 'SKILL.md').write_text('## 知识边界与诚实局限\n不知道的事情不要编造。\n', encoding='utf-8')
            with patch.object(bt, 'ROOT', p), patch.object(bt, 'load_exam', return_value=exam), patch.object(bt, 'call_llm', side_effect=['synthetic answer', json.dumps(judge)]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bt.run_mutation(p, 'mock'), 0)
            result = json.loads((p / 'references/behavior-mutation.json').read_text(encoding='utf-8'))
            self.assertEqual(result['exam_id'], exam['exam_id'])


if __name__ == '__main__':
    unittest.main()
