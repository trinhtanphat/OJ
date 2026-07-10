from datetime import timedelta

from django.conf import settings
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import Contest, ContestProblem, Language, Submission, SubmissionTestCase
from judge.models.problem import ProblemTestcaseResultAccess
from judge.models.tests.util import CommonDataMixin, create_problem, create_user
from judge.views.widgets import csrf_failure


class SubmissionTestcaseStatusAccessTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.problem = create_problem(
            code='hidden_case_status',
            is_public=True,
            testcase_result_visibility_mode=ProblemTestcaseResultAccess.ONLY_SUBMISSION_RESULT,
        )
        cls.submission = Submission.objects.create(
            user=cls.users['normal'].profile,
            problem=cls.problem,
            language=Language.get_python3(),
            status='D',
            result='WA',
            case_points=0,
            case_total=1,
        )
        SubmissionTestCase.objects.create(
            submission=cls.submission,
            case=1,
            status='WA',
            time=0.01,
            memory=1024,
            points=0,
            total=1,
        )

    def test_problem_editor_overrides_hidden_testcase_result_policy(self):
        self.assertTrue(self.problem.is_testcase_result_accessible_by(self.users['superuser']))
        self.assertTrue(self.problem.is_testcase_result_accessible_by(self.users['staff_problem_edit_all']))
        self.assertFalse(self.problem.is_testcase_result_accessible_by(self.users['normal']))

        self.problem.testcase_result_visibility_mode = ProblemTestcaseResultAccess.ALL_TEST_CASE
        self.assertTrue(self.problem.is_testcase_result_accessible_by(self.users['normal']))

    def test_problem_editor_sees_hidden_case_rows_on_detail_and_ajax_fragment(self):
        detail_url = reverse('submission_status', args=[self.submission.id])
        fragment_url = reverse('submission_testcases_query')

        self.client.force_login(self.users['staff_problem_edit_all'])
        detail = self.client.get(detail_url)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'submissions-status-table')
        self.assertContains(detail, 'Test case #1')

        fragment = self.client.get(fragment_url, {'id': self.submission.id})
        self.assertEqual(fragment.status_code, 200)
        self.assertContains(fragment, 'submissions-status-table')
        self.assertContains(fragment, 'Test case #1')

    def test_submission_owner_keeps_hidden_case_rows_hidden(self):
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('submission_status', args=[self.submission.id]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'submissions-status-table')

    def test_cppro_problem_manager_can_view_cases_during_frozen_contest(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key='hidden_case_frozen',
            name='Frozen hidden-case contest',
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(minutes=15),
            frozen_last_minutes=30,
            is_visible=True,
        )
        ContestProblem.objects.create(contest=contest, problem=self.problem, points=100, order=1)
        self.submission.contest_object = contest
        self.submission.save(update_fields=['contest_object'])
        self.assertTrue(contest.is_frozen)

        manager = create_user(
            username='hidden_case_problem_manager',
            user_permissions=('edit_own_problem',),
        )
        self.problem.authors.add(manager.profile)

        detail_url = '/api/cppro/submissions/%d' % self.submission.id
        self.client.force_login(manager)
        manager_response = self.client.get(detail_url, {'includeTests': 'all', 'admin': '1'})

        self.assertEqual(manager_response.status_code, 200)
        manager_payload = manager_response.json()
        self.assertTrue(manager_payload['can_view_test_details'])
        self.assertEqual(manager_payload['material_access_scope'], 'admin')
        self.assertEqual(len(manager_payload['testCases']), 1)
        self.assertEqual(manager_payload['testCases'][0]['verdict'], 'WA')

        self.client.force_login(self.users['normal'])
        user_response = self.client.get(detail_url, {'includeTests': 'all'})

        self.assertEqual(user_response.status_code, 200)
        user_payload = user_response.json()
        self.assertFalse(user_payload['can_view_test_details'])
        self.assertEqual(user_payload['material_access_scope'], 'contest-restricted')
        self.assertEqual(user_payload['testCases'], [])


class CpproCsrfFailureTestCase(TestCase):
    def test_cppro_auth_me_bootstraps_and_enforces_csrf(self):
        client = Client(enforce_csrf_checks=True)
        bootstrap = client.get('/api/cppro/auth/me')

        self.assertEqual(bootstrap.status_code, 200)
        self.assertIn(settings.CSRF_COOKIE_NAME, bootstrap.cookies)
        csrf_token = bootstrap.cookies[settings.CSRF_COOKIE_NAME].value

        rejected = client.post('/api/cppro/auth/logout', data='{}', content_type='application/json')
        self.assertEqual(rejected.status_code, 403)
        self.assertJSONEqual(rejected.content.decode('utf-8'), {
            'message': 'CSRF token missing or incorrect.',
        })

        accepted = client.post(
            '/api/cppro/auth/logout',
            data='{}',
            content_type='application/json',
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(accepted.status_code, 200)

    def test_cppro_csrf_failure_stays_a_json_403(self):
        request = RequestFactory().post('/api/cppro/submissions')
        response = csrf_failure(request)

        self.assertEqual(response.status_code, 403)
        self.assertJSONEqual(response.content.decode('utf-8'), {
            'message': 'CSRF token missing or incorrect.',
        })

    def test_legacy_csrf_failure_keeps_browser_redirect(self):
        request = RequestFactory().post('/problem/hidden_case_status/edit')
        response = csrf_failure(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/problem/hidden_case_status/edit')
