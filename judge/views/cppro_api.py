import json
import os
import re
from io import BytesIO
from datetime import datetime, timedelta, timezone as datetime_timezone
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from django.apps import apps
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.shortcuts import get_current_site
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.mail.backends.smtp import EmailBackend
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Prefetch, Q, Sum
from django.http import JsonResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from judge.models import BlogPost, Contest, ContestAnnouncement, ContestParticipation, Language, MiscConfig, Organization, OrganizationRequest, Problem, Profile, Submission, SubmissionSource
from judge.models.contest import ContestProblem, ContestSubmission
from judge.models.problem import ProblemGroup, ProblemType
from judge.models.problem_data import CHECKERS, GRADERS, ProblemData, ProblemTestCase
from judge.models.runtime import RuntimeVersion
from judge.models.ticket import Ticket
from judge.utils.problem_data import ProblemDataCompiler
from judge.views.register import CustomRegistrationForm, RegistrationView


CPPRO_META_MARKER = 'CPPRO_META:'
CPPRO_SITE_SETTINGS_KEY = 'cppro_site_settings'
CPPRO_SMTP_SETTINGS_KEY = 'cppro_smtp_settings'
CPPRO_HOME_TRAFFIC_KEY = 'cppro_home_traffic'
CPPRO_RATING_SETTINGS_KEY = 'cppro_rating_settings'
CPPRO_POST_REGISTRY_KEY = 'cppro_post_registry'


def _cppro_post_registry():
    stored = _cppro_config_value(CPPRO_POST_REGISTRY_KEY)
    rows = stored.get('rows') if isinstance(stored.get('rows'), dict) else {}
    return {
        str(post_id): str(kind)
        for post_id, kind in rows.items()
        if str(post_id).isdigit() and str(kind) in {'community', 'announcement'}
    }


def _registered_post_ids(kind):
    return {
        int(post_id)
        for post_id, registered_kind in _cppro_post_registry().items()
        if registered_kind == kind
    }


def _register_cppro_post(post, kind):
    if kind not in {'community', 'announcement'}:
        raise ValueError('Unknown CPPro post type.')
    registry = _cppro_post_registry()
    registry[str(post.id)] = kind
    _save_cppro_config_value(CPPRO_POST_REGISTRY_KEY, {'rows': registry})


def _unregister_cppro_post(post):
    registry = _cppro_post_registry()
    if registry.pop(str(post.id), None) is not None:
        _save_cppro_config_value(CPPRO_POST_REGISTRY_KEY, {'rows': registry})


def _cppro_config_value(key):
    row = MiscConfig.objects.filter(key=key).order_by('-id').first()
    if not row or not row.value:
        return {}
    try:
        value = json.loads(row.value)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_cppro_config_value(key, value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    row = MiscConfig.objects.filter(key=key).order_by('id').first()
    if row:
        row.value = encoded
        row.save(update_fields=['value'])
    else:
        MiscConfig.objects.create(key=key, value=encoded)


def _home_traffic_snapshot(config=None):
    config = config if isinstance(config, dict) else _cppro_config_value(CPPRO_HOME_TRAFFIC_KEY)
    now = timezone.now()
    cutoff = now.timestamp() - 300
    presence = config.get('presence') if isinstance(config.get('presence'), dict) else {}
    active_presence = {}
    for client_id, seen_at in presence.items():
        try:
            if float(seen_at) >= cutoff:
                active_presence[str(client_id)[:96]] = float(seen_at)
        except (TypeError, ValueError):
            continue
    return {
        'visits': max(0, int(config.get('visits') or 0)),
        'pageViews': max(0, int(config.get('pageViews') or 0)),
        'presence': active_presence,
    }


def _home_activity_rows(submission_queryset=None):
    today = timezone.localdate()
    first_day = today - timedelta(days=13)
    totals = {str(first_day + timedelta(days=offset)): {'submissions': 0, 'accepted': 0} for offset in range(14)}
    query = submission_queryset if submission_queryset is not None else Submission.objects.all()
    submissions = query.filter(date__date__gte=first_day).values_list('date', 'result', 'status')
    for submitted_at, result, status in submissions:
        if not submitted_at:
            continue
        day = timezone.localtime(submitted_at).date().isoformat()
        row = totals.get(day)
        if row is None:
            continue
        row['submissions'] += 1
        if (result or status or '').upper() == 'AC':
            row['accepted'] += 1
    return [
        {
            'day': day,
            'submissions': values['submissions'],
            'accepted': values['accepted'],
            'failed': max(0, values['submissions'] - values['accepted']),
        }
        for day, values in totals.items()
    ]


def _post_row(post, post_type='announcement'):
    author = post.authors.select_related('user').first()
    username = getattr(getattr(author, 'user', None), 'username', '') or 'system'
    full_name = _author_name(author) if author else 'ITCoder'
    return {
        'id': post.id,
        'slug': post.slug,
        'title': post.title,
        'post_type': post_type,
        'author_username': username,
        'author_full_name': full_name,
        'author_avatar_url': _profile_avatar(author) if author else '',
        'published_at': post.publish_on.isoformat() if post.publish_on else '',
        'content': post.content or '',
        'excerpt': post.summary or '',
        'og_image': post.og_image or '',
        'vote_score': int(post.score or 0),
        'comment_count': 0,
    }


def _visible_registered_posts(kind, request_user, limit=24):
    """Return only posts whose native BlogPost visibility allows this viewer."""
    post_ids = _registered_post_ids(kind)
    if not post_ids:
        return []
    posts = (
        BlogPost.objects
        .filter(id__in=post_ids, visible=True, publish_on__lte=timezone.now())
        .prefetch_related('authors__user')
        .order_by('-sticky', '-publish_on', '-id')
    )
    return [post for post in posts if post.can_see(request_user)][:limit]


def _contest_announcement_row(announcement):
    contest = announcement.contest
    return {
        'id': 'contest-%s-announcement-%s' % (contest.id, announcement.id),
        'slug': 'contest-%s-announcement-%s' % (contest.key, announcement.id),
        'title': announcement.title,
        'post_type': 'contest',
        'author_username': 'system',
        'author_full_name': contest.name,
        'published_at': announcement.date.isoformat() if announcement.date else '',
        'content': announcement.description or '',
        'excerpt': announcement.description or '',
        'contest_slug': contest.key,
        'comment_count': 0,
    }


def _quiz_row(quiz):
    return {
        'id': quiz.id,
        'code': quiz.code,
        'slug': quiz.code,
        'title': quiz.name,
        'description': quiz.description or '',
        'time_limit_minutes': quiz.time_limit,
        'attempt_limit': quiz.max_attempts,
        'question_count': int(getattr(quiz, 'question_count', 0) or 0),
        'total_points': float(getattr(quiz, 'total_points', 0) or 0),
        'start_time': quiz.start_time.isoformat() if quiz.start_time else '',
        'end_time': quiz.end_time.isoformat() if quiz.end_time else '',
        'integrity_monitoring': bool(quiz.integrity_monitoring),
    }


def _platform_site_defaults(request):
    current_site = get_current_site(request)
    domain = str(getattr(current_site, 'domain', '') or request.get_host()).strip()
    name = str(getattr(current_site, 'name', '') or getattr(settings, 'SITE_NAME', '') or 'Online Judge').strip()
    scheme = 'https' if request.is_secure() else 'http'
    return {
        'name': name or 'Online Judge',
        'domain': domain,
        'frontendUrl': '%s://%s' % (scheme, domain),
        'logoUrl': '',
        'faviconUrl': '',
        'ogImageUrl': '',
        'footerCopyright': '',
        'supportEmail': '',
        'supportPhone': '',
        'footerLocation': '',
        'facebookUrl': '',
        'youtubeUrl': '',
        'tiktokUrl': '',
        'maxTestcasesShown': 50,
        'topbarFeatures': {},
    }


def _safe_site_url(value):
    raw = str(value or '').strip()
    if not raw:
        return ''
    if raw.startswith('/') and not raw.startswith('//'):
        return raw[:500]
    parsed = urlsplit(raw)
    if parsed.scheme in ('http', 'https') and parsed.netloc:
        return raw[:500]
    raise ValueError('Only HTTP(S) or site-relative URLs are allowed.')


def _normalize_platform_site_settings(request, payload=None):
    defaults = _platform_site_defaults(request)
    stored = _cppro_config_value(CPPRO_SITE_SETTINGS_KEY)
    result = dict(defaults)
    result.update({key: value for key, value in stored.items() if key in result})
    if payload is not None:
        for key in (
            'name', 'domain', 'footerCopyright', 'supportEmail', 'supportPhone',
            'footerLocation',
        ):
            if key in payload:
                result[key] = str(payload.get(key) or '').strip()[:500]
        for key in ('frontendUrl', 'logoUrl', 'faviconUrl', 'ogImageUrl', 'facebookUrl', 'youtubeUrl', 'tiktokUrl'):
            if key in payload:
                result[key] = _safe_site_url(payload.get(key))
        if 'maxTestcasesShown' in payload:
            result['maxTestcasesShown'] = max(1, min(500, int(payload.get('maxTestcasesShown') or 50)))
        if 'topbarFeatures' in payload and isinstance(payload.get('topbarFeatures'), dict):
            result['topbarFeatures'] = {
                str(key)[:64]: bool(value)
                for key, value in payload['topbarFeatures'].items()
            }
    result['name'] = result['name'] or defaults['name']
    result['domain'] = result['domain'] or defaults['domain']
    result['frontendUrl'] = result['frontendUrl'] or defaults['frontendUrl']
    result['footerCopyright'] = result['footerCopyright'] or ''
    return result


def _smtp_settings():
    stored = _cppro_config_value(CPPRO_SMTP_SETTINGS_KEY)
    try:
        port = int(stored.get('port') or 587)
    except (TypeError, ValueError):
        port = 587
    return {
        'host': str(stored.get('host') or '').strip()[:255],
        'port': max(1, min(65535, port)),
        'secure': bool(stored.get('secure')),
        'startTls': bool(stored.get('startTls', True)),
        'username': str(stored.get('username') or '').strip()[:255],
        'password': str(stored.get('password') or ''),
        'from': str(stored.get('from') or '').strip()[:254],
        'ehloDomain': str(stored.get('ehloDomain') or 'localhost').strip()[:255] or 'localhost',
    }


def _smtp_public_settings():
    value = _smtp_settings()
    return {
        'host': value['host'],
        'port': value['port'],
        'secure': value['secure'],
        'startTls': value['startTls'],
        'username': value['username'],
        'from': value['from'],
        'ehloDomain': value['ehloDomain'],
        'passwordConfigured': bool(value['password']),
        'configured': bool(value['host'] and value['from']),
    }


def _smtp_connection(settings_value):
    return EmailBackend(
        host=settings_value['host'],
        port=settings_value['port'],
        username=settings_value['username'],
        password=settings_value['password'],
        use_ssl=settings_value['secure'],
        use_tls=settings_value['startTls'],
        timeout=12,
    )


class CpproSMTPEmailBackend(EmailBackend):
    """Use the staff-managed SMTP configuration for every Django email send."""

    def __init__(self, *args, **kwargs):
        smtp_settings = _smtp_settings()
        if smtp_settings['host']:
            kwargs.update({
                'host': smtp_settings['host'],
                'port': smtp_settings['port'],
                'username': smtp_settings['username'],
                'password': smtp_settings['password'],
                'use_ssl': smtp_settings['secure'],
                'use_tls': smtp_settings['startTls'],
                'timeout': 12,
            })
        super().__init__(*args, **kwargs)


def _is_platform_admin(user):
    return bool(user.is_authenticated and user.is_staff)


def _require_platform_admin(request):
    if _is_platform_admin(request.user):
        return None
    return _json_error('Administrator access required.', 403)


def _author_name(profile):
    user = getattr(profile, 'user', None)
    if not user:
        return ''
    full_name = user.get_full_name() if hasattr(user, 'get_full_name') else ''
    return full_name or getattr(profile, 'username_display_override', '') or getattr(user, 'username', '') or ''


def _difficulty(problem):
    points = float(problem.points or 0)
    if points >= 250:
        return 'hard'
    if points >= 100:
        return 'medium'
    return 'easy'


def _read_cppro_meta(profile):
    notes = profile.notes or ''
    for line in notes.splitlines():
        if line.startswith(CPPRO_META_MARKER):
            try:
                value = json.loads(line[len(CPPRO_META_MARKER):].strip() or '{}')
                return value if isinstance(value, dict) else {}
            except ValueError:
                return {}
    return {}


def _write_cppro_meta(profile, meta):
    lines = [line for line in (profile.notes or '').splitlines() if not line.startswith(CPPRO_META_MARKER)]
    clean_meta = {key: value for key, value in meta.items() if value not in (None, '')}
    if clean_meta:
        lines.append(CPPRO_META_MARKER + json.dumps(clean_meta, ensure_ascii=False, sort_keys=True))
    profile.notes = '\n'.join(lines).strip() or None


def _profile_avatar(profile):
    return str(_read_cppro_meta(profile).get('avatar_url') or '')


def _role_for(profile):
    user = profile.user
    if user.is_superuser:
        return 'admin'
    if user.is_staff:
        return 'teacher'
    return profile.display_rank or 'user'


def _problem_row(problem, submission_counts, user_progress=None):
    authors = list(problem.authors.select_related('user').all())
    author = authors[0] if authors else None
    types = list(problem.types.all())
    group = getattr(problem, 'group', None)
    memory_limit = int(problem.memory_limit or 0)
    memory_mb = max(1, int(round(memory_limit / 1024))) if memory_limit > 1024 else (memory_limit or 256)
    progress = (user_progress or {}).get(problem.id, {})
    return {
        'id': problem.id,
        'code': problem.code,
        'external_id': problem.code,
        'slug': problem.code,
        'title': problem.name,
        'description': problem.description or '',
        'difficulty': _difficulty(problem),
        'score': float(problem.points or 0),
        'full_score': float(problem.points or 0),
        'source': getattr(group, 'full_name', '') or getattr(group, 'name', '') or 'LCOJ',
        'timeLimitMs': int(float(problem.time_limit or 1) * 1000),
        'memoryLimitMb': memory_mb,
        'accepted': int(problem.user_count or 0),
        'accepted_users': int(problem.user_count or 0),
        'submissions': int(submission_counts.get(problem.id, 0)),
        'attempted_users': int(submission_counts.get(problem.id, 0)),
        'tags': [
            {'id': item.id, 'name': item.full_name or item.name, 'slug': item.name}
            for item in types
        ],
        'allowed_languages': list(problem.allowed_languages.order_by('key').values_list('key', flat=True)),
        'author_username': getattr(getattr(author, 'user', None), 'username', '') if author else '',
        'author_full_name': _author_name(author) if author else '',
        'user_solved': bool(progress.get('solved')),
        'user_attempts': int(progress.get('attempts') or 0),
        'my_status': progress.get('latest_verdict') or '',
        'my_best_verdict': 'AC' if progress.get('solved') else (progress.get('best_verdict') or ''),
    }


def _contest_status(contest):
    if not contest.is_visible:
        return 'draft'
    now = timezone.now()
    if contest.start_time and now < contest.start_time:
        return 'upcoming'
    if contest.end_time and now > contest.end_time:
        return 'finished'
    return 'live'


def _contest_problem_row(contest_problem, submission_counts):
    problem = contest_problem.problem
    memory_limit = int(problem.memory_limit or 0)
    memory_mb = max(1, int(round(memory_limit / 1024))) if memory_limit > 1024 else (memory_limit or 256)
    return {
        'id': problem.id,
        'problem': problem.id,
        'external_id': problem.code,
        'problem_code': problem.code,
        'problem_slug_snapshot': problem.code,
        'title': problem.name,
        'problem_title_snapshot': problem.name,
        'order_index': int(contest_problem.order or 0),
        'order_idx': int(contest_problem.order or 0),
        'points': int(contest_problem.points or problem.points or 0),
        'rating_points': int(contest_problem.points or problem.points or 0),
        'difficulty': _difficulty(problem),
        'difficulty_tag': _difficulty(problem),
        'time_limit_ms_snapshot': int(float(problem.time_limit or 1) * 1000),
        'memory_limit_mb_snapshot': memory_mb,
        'submissions': int(submission_counts.get(problem.id, 0)),
        'accepted': int(problem.user_count or 0),
    }


def _contest_participation_row(participation, frozen=False):
    if not participation:
        return None
    if participation.is_disqualified:
        status = 'disqualified'
    elif participation.pre_registered:
        status = 'registered'
    elif participation.ended:
        status = 'finished'
    else:
        status = 'active'
    if participation.spectate:
        participation_type = 'spectate'
    elif participation.virtual:
        participation_type = 'virtual'
    else:
        participation_type = 'official'
    score = participation.frozen_score if frozen else participation.score
    cumtime = participation.frozen_cumtime if frozen else participation.cumtime
    return {
        'id': participation.id,
        'status': status,
        'virtual': int(participation.virtual or 0),
        'participation_type': participation_type,
        'score': float(score or 0),
        'cumtime': int(cumtime or 0),
        'is_disqualified': bool(participation.is_disqualified),
        'started_at': participation.real_start.isoformat() if participation.real_start else '',
        'end_time': participation.end_time.isoformat() if participation.end_time else '',
    }


def _contest_participation_counts(contest_ids):
    """Return separate official and virtual participation totals by contest."""
    official_counts = {}
    virtual_counts = {}
    for contest_id, virtual in (
        ContestParticipation.objects
        .filter(contest_id__in=contest_ids)
        .values_list('contest_id', 'virtual')
    ):
        if virtual == ContestParticipation.LIVE:
            official_counts[contest_id] = official_counts.get(contest_id, 0) + 1
        elif virtual > ContestParticipation.LIVE:
            virtual_counts[contest_id] = virtual_counts.get(contest_id, 0) + 1
    return official_counts, virtual_counts


def _contest_participant_row(participation, frozen=False):
    profile = participation.user
    return {
        **(_contest_participation_row(participation, frozen=frozen) or {}),
        'user_id': profile.id,
        'username': profile.user.username,
        'full_name': _author_name(profile),
        'avatar_url': _profile_avatar(profile),
        'joined_at': participation.real_start.isoformat() if participation.real_start else '',
    }


def _contest_scoreboard_access(contest, request_user):
    can_edit = bool(
        request_user.is_authenticated
        and (_is_platform_admin(request_user) or request_user.is_superuser or contest.is_editable_by(request_user))
    )
    can_view_full = bool(can_edit or contest.can_see_full_scoreboard(request_user))
    return can_view_full, can_edit, bool(contest.is_frozen and not can_edit)


def _can_view_contest_problems(contest, request_user, profile=None):
    # Match the native contest page: problem identities stay sealed until the
    # viewer is actively participating, the contest ends, or the viewer is a
    # contest/platform manager.
    if contest.ended:
        return True
    if not request_user.is_authenticated:
        return False
    if request_user.is_staff or request_user.is_superuser:
        return True
    profile = profile or _current_profile_from_user(request_user)
    if not profile:
        return False
    if contest.is_in_contest(request_user):
        return True
    return profile.id in contest.editor_ids or profile.id in contest.tester_ids


def _contest_row(
    contest,
    participation_counts,
    virtual_participation_counts,
    submission_counts,
    profile=None,
    request_user=None,
):
    contest_problems = list(
        contest.contest_problems
        .select_related('problem')
        .order_by('order', 'id')
    )
    my_participation = None
    if profile:
        active_participation = profile.current_contest
        if active_participation and active_participation.contest_id == contest.id:
            my_participation = active_participation
        else:
            # A pre-registration is meaningful before the contest starts, while
            # an older live participation must not keep the participant joined.
            my_participation = (
                ContestParticipation.objects
                .filter(
                    contest=contest,
                    user=profile,
                    real_start=datetime(1970, 1, 1, tzinfo=datetime_timezone.utc),
                )
                .order_by('-id')
                .first()
            )
    duration_minutes = 0
    if contest.time_limit:
        duration_minutes = int(contest.time_limit.total_seconds() // 60)
    elif contest.start_time and contest.end_time:
        duration_minutes = int((contest.end_time - contest.start_time).total_seconds() // 60)
    participant_count = int(participation_counts.get(contest.id, 0))
    virtual_participants = int(virtual_participation_counts.get(contest.id, 0))
    format_config = dict(contest.format_config or {}) if isinstance(contest.format_config, dict) else {}
    format_config['allowedLanguages'] = _cppro_config_value('cppro_contest_%s' % contest.id).get('allowedLanguages', [])
    format_labels = dict(Contest._meta.get_field('format_name').choices or [])
    freeze_minutes = max(0, int(contest.frozen_last_minutes or 0))
    freeze_time = contest.frozen_time if freeze_minutes and contest.end_time else None
    frozen_scoreboard = bool(contest.is_frozen)
    can_view_problems = False
    if request_user is not None:
        _, _, frozen_scoreboard = _contest_scoreboard_access(contest, request_user)
        can_view_problems = _can_view_contest_problems(contest, request_user, profile)
    return {
        'id': contest.id,
        'external_id': contest.key,
        'slug': contest.key,
        'title': contest.name,
        'description': contest.description or contest.summary or '',
        'visibility': 'private' if contest.is_private or contest.is_organization_private else 'public',
        'access_type': 'private' if contest.is_private or contest.access_code else 'open',
        'format': contest.format_name or 'contest',
        'format_label': format_labels.get(contest.format_name, contest.format_name or 'Contest'),
        'format_config': format_config,
        'freeze_minutes': freeze_minutes,
        'freeze_time': freeze_time.isoformat() if freeze_time else '',
        'frozen': bool(contest.is_frozen),
        'freeze_supported': contest.format_name in {'icpc', 'vnoj'},
        'scoreboard_visibility': contest.scoreboard_visibility,
        'show_submission_list': bool(contest.show_submission_list),
        'allow_virtual': not bool(contest.disallow_virtual),
        'is_rated': bool(contest.is_rated),
        'start_time': contest.start_time.isoformat() if contest.start_time else '',
        'end_time': contest.end_time.isoformat() if contest.end_time else '',
        'duration_minutes': max(0, duration_minutes),
        'participant_count': participant_count,
        'virtual_participants': virtual_participants,
        'participant_total': participant_count + virtual_participants,
        'problem_count': len(contest_problems),
        'status': _contest_status(contest),
        'phase': _contest_status(contest),
        'problems': [
            _contest_problem_row(item, submission_counts)
            for item in contest_problems
        ] if can_view_problems else [],
        'problems_available': bool(can_view_problems),
        'myParticipant': _contest_participation_row(my_participation, frozen=frozen_scoreboard),
    }


def _organization_visibility(organization):
    if organization.is_unlisted:
        return 'private'
    if organization.is_open:
        return 'public'
    return 'protected'


def _organization_admin_usernames(organization, include_unlisted=False):
    query = organization.admins.select_related('user')
    if not include_unlisted:
        query = query.filter(is_unlisted=False)
    return list(query.values_list('user__username', flat=True))


def _organization_membership(organization, profile):
    if not profile:
        return None, None
    if organization.admins.filter(pk=profile.pk).exists():
        return 'admin', 'active'
    if profile.organizations.filter(pk=organization.pk).exists():
        return 'member', 'active'
    request = OrganizationRequest.objects.filter(user=profile, organization=organization).order_by('-time').first()
    if request:
        state = {'P': 'pending', 'A': 'active', 'R': 'rejected'}.get(request.state, request.state or 'pending')
        return None, state
    return None, None


def _can_view_organization(organization, profile, request_user):
    if request_user.is_staff:
        return True
    if not organization.is_unlisted:
        return True
    if not profile:
        return False
    return (
        organization.admins.filter(pk=profile.pk).exists()
        or profile.organizations.filter(pk=organization.pk).exists()
    )


def _can_manage_organization(organization, profile, request_user):
    return bool(request_user.is_staff or (profile and organization.admins.filter(pk=profile.pk).exists()))


def _organization_row(organization, profile, problem_counts, contest_counts, include_unlisted_admins=False):
    my_role, my_status = _organization_membership(organization, profile)
    members = list(organization.members.select_related('user').all())
    member_count = int(organization.member_count or 0) or len(members)
    total_rating = sum(int(member.rating or 0) for member in members)
    admins = _organization_admin_usernames(organization, include_unlisted=include_unlisted_admins)
    return {
        'id': organization.id,
        'slug': organization.slug,
        'name': organization.name,
        'short_name': organization.short_name,
        'description': organization.about or '',
        'visibility': _organization_visibility(organization),
        'member_count': member_count,
        'problem_count': int(problem_counts.get(organization.id, 0)),
        'contest_count': int(contest_counts.get(organization.id, 0)),
        'total_rating': total_rating,
        'my_role': my_role,
        'my_status': my_status,
        'creator_username': admins[0] if admins else '',
        'admin_usernames': admins,
        'created_at': organization.creation_date.isoformat() if organization.creation_date else '',
        'updated_at': organization.creation_date.isoformat() if organization.creation_date else '',
    }


def _organization_member_row(profile, role='member', include_email=False):
    user = profile.user
    row = {
        'user_id': user.id,
        'username': user.username,
        'full_name': user.get_full_name() or profile.username_display_override or user.username,
        'role': role,
        'status': 'active',
        'joined_at': getattr(user, 'date_joined', None).isoformat() if getattr(user, 'date_joined', None) else '',
    }
    if include_email:
        row['email'] = user.email or ''
    return row


def _organization_problem_row(problem):
    return {
        'id': problem.id,
        'external_id': problem.code,
        'slug': problem.code,
        'title': problem.name,
        'difficulty': _difficulty(problem),
        'visibility': 'public' if problem.is_public else 'private',
        'updated_at': problem.date.isoformat() if problem.date else '',
    }


def _organization_contest_row(contest):
    return {
        'id': contest.id,
        'external_id': contest.key,
        'slug': contest.key,
        'title': contest.name,
        'status': _contest_status(contest),
        'visibility': 'private' if contest.is_private or contest.is_organization_private else 'public',
        'start_time': contest.start_time.isoformat() if contest.start_time else '',
        'end_time': contest.end_time.isoformat() if contest.end_time else '',
    }


def _profile_organization(profile, include_unlisted=False):
    if not include_unlisted and hasattr(profile, 'public_organizations'):
        organizations = profile.public_organizations
    else:
        organizations = profile.organizations.all()
    for organization in organizations:
        if include_unlisted or not organization.is_unlisted:
            return organization
    return None


def _profile_row(profile, include_unlisted_organization=False, submission_count=None):
    user = profile.user
    role = _role_for(profile)
    full_name = user.get_full_name() or profile.username_display_override or user.username
    badges = list(profile.badges.values_list('name', flat=True))
    org = _profile_organization(profile, include_unlisted=include_unlisted_organization)
    meta = _read_cppro_meta(profile)
    return {
        'id': profile.id,
        'user_id': user.id,
        'username': user.username,
        'full_name': full_name,
        'email': user.email or '',
        'avatar_url': _profile_avatar(profile),
        'bio': profile.about or '',
        'role': role,
        'rank_name': 'Admin' if role == 'admin' else ('Teacher' if role == 'teacher' else (profile.display_rank or 'Member')),
        'rating_tier': profile.display_rank or role,
        'rating': profile.rating or 0,
        'contest_rating': profile.rating or 0,
        'max_rating': profile.rating or 0,
        'score': float(profile.points or 0),
        'total_score': float(profile.points or 0),
        'points': float(profile.points or 0),
        'pp_score': float(profile.performance_points or 0),
        'solved': int(profile.problem_count or 0),
        'accepted': int(profile.problem_count or 0),
        'submissions': int(
            Submission.objects.filter(user=profile).count()
            if submission_count is None else submission_count
        ),
        'current_streak': 0,
        'longest_streak': 0,
        'is_teacher': user.is_staff or user.is_superuser,
        'is_ultra': role == 'admin',
        'is_ultra_max': role == 'admin',
        'membership_tier': 'free',
        'badges': badges,
        'tags': [item for item in [role, 'Teacher' if user.is_staff else '', 'Admin' if user.is_superuser else ''] if item],
        'organization_name': getattr(org, 'name', '') if org else '',
        'organization_slug': getattr(org, 'slug', '') if org else '',
        'created_at': user.date_joined.isoformat() if user.date_joined else '',
        'streak_timezone': profile.timezone or 'Asia/Bangkok',
        'favorite_language': str(meta.get('favorite_language') or ''),
    }


def _public_profile_row(profile, submission_count=0):
    row = _profile_row(profile, submission_count=submission_count)
    row.pop('email', None)
    return row


def _profile_row_for_viewer(profile, request_user, submission_count=None):
    can_view_email = bool(
        request_user.is_authenticated
        and (request_user.id == profile.user_id or request_user.is_staff)
    )
    row = _profile_row(
        profile,
        include_unlisted_organization=can_view_email,
        submission_count=submission_count,
    )
    if not can_view_email:
        row.pop('email', None)
    return row


def _auth_user_row(profile):
    row = _profile_row(profile, include_unlisted_organization=True)
    role = row['role']
    return {
        'id': row['user_id'],
        'username': row['username'],
        'email': row['email'],
        'full_name': row['full_name'],
        'avatar_url': row['avatar_url'] or None,
        'bio': row['bio'] or None,
        'role': role,
        'roles': [role],
        'is_teacher': bool(row['is_teacher']),
        'membership_tier': row['membership_tier'],
        'membership_expires_at': None,
        'streak_timezone': row['streak_timezone'],
        'streak_timezone_changed_at': None,
        'favorite_language': row['favorite_language'],
        'rating': row['rating'],
        'rank_name': row['rank_name'],
        'solved': row['solved'],
        'score': row['score'],
        'pp_score': row['pp_score'],
        'streak': row['current_streak'],
        'max_streak': row['longest_streak'],
        'submissions': row['submissions'],
    }


def _submission_row(submission):
    problem = submission.problem
    contest = getattr(submission, 'contest_object', None)
    return {
        'id': submission.id,
        'username': submission.user.user.username,
        'full_name': submission.user.user.get_full_name() or submission.user.user.username,
        'problem_id': problem.id,
        'problem_slug': problem.code,
        'problem_external_id': problem.code,
        'problem_title': problem.name,
        'contest_id': submission.contest_object_id,
        'contest_external_id': getattr(contest, 'key', '') if contest else '',
        'contest_title': getattr(contest, 'name', '') if contest else '',
        'language': submission.language.name,
        'verdict': submission.result or submission.status or 'PENDING',
        'score': float(submission.points or 0),
        'max_score': float(problem.points or 100),
        'runtime': float(submission.time or 0) * 1000,
        'memory': float(submission.memory or 0),
        'created_at': submission.date.isoformat() if submission.date else '',
    }


def _submission_testcase_row(testcase):
    return {
        'id': testcase.id,
        'index': int(testcase.case or 0) + 1,
        'case_id': int(testcase.case or 0),
        'verdict': testcase.status or 'NOT_RUN',
        'status': testcase.status or 'NOT_RUN',
        'runtime': float(testcase.time or 0) * 1000,
        'memory': float(testcase.memory or 0),
        'point': float(testcase.points or 0),
        'score': float(testcase.points or 0),
        'message': testcase.feedback or testcase.extended_feedback or '',
        'actual': testcase.output or '',
        'stderr': testcase.extended_feedback or '',
    }


def _submission_detail_row(submission, request_user, include_tests=False, admin_context=False):
    row = _submission_row(submission)
    can_view_source, material_scope = _can_view_submission_materials(
        submission,
        request_user,
        admin_context=admin_context,
    )
    can_view_tests = can_view_source
    source = ''
    if can_view_source:
        try:
            source = submission.source.source or ''
        except SubmissionSource.DoesNotExist:
            source = ''
    testcase_model = apps.get_model('judge', 'SubmissionTestCase')
    testcases = list(testcase_model.objects.filter(submission=submission).order_by('case', 'id')) if can_view_tests and (include_tests or can_view_source) else []
    can_view_judge_log = bool(
        can_view_source
        and (
            submission.status != 'IE'
            or request_user.has_perm('judge.view_all_submission')
            or _can_manage_problem(request_user, submission.problem)
        )
    )
    row.update({
        'problem_id': submission.problem_id,
        'contest_id': submission.contest_object_id,
        'code': source,
        'can_view_source': bool(can_view_source),
        # Internal errors can contain paths and infrastructure diagnostics.
        # Native DMOJ only shows them to problem managers; normal compile or
        # checker feedback remains on the source-material boundary.
        'judge_log': (submission.error or '') if can_view_judge_log else '',
        'testCases': [_submission_testcase_row(testcase) for testcase in testcases],
        'testcase_count': max(int(submission.case_total or 0), len(testcases)),
        'can_view_test_details': bool(testcases),
        'visible_testcase_scope': 'all' if testcases else 'summary',
        'max_testcases_shown': len(testcases) if testcases else 0,
        'sample_only': False,
        'custom_run': False,
        'result_hidden': False,
        'material_access_scope': material_scope,
    })
    return row


def _submission_list_payload(request):
    query = (
        _visible_submission_queryset(request.user)
        .select_related('problem', 'language', 'user__user', 'contest_object')
    )
    profile = _current_profile(request)
    if request.GET.get('scope') == 'mine':
        if not profile:
            query = query.none()
        else:
            query = query.filter(user=profile)
    verdict = str(request.GET.get('verdict') or '').strip().upper()
    if verdict and verdict != 'ALL':
        query = query.filter(Q(result__iexact=verdict) | Q(status__iexact=verdict))
    language = str(request.GET.get('language') or '').strip()
    if language and language.lower() != 'all':
        query = query.filter(Q(language__key__iexact=language) | Q(language__name__iexact=language))
    search = str(request.GET.get('q') or '').strip()
    if search:
        query = query.filter(
            Q(problem__code__icontains=search)
            | Q(problem__name__icontains=search)
            | Q(user__user__username__icontains=search)
            | Q(language__name__icontains=search)
            | Q(language__key__icontains=search)
            | Q(result__icontains=search)
            | Q(status__icontains=search)
        )
    total = query.count()
    today = query.filter(date__date=timezone.localdate()).count()
    verdicts = {}
    languages = {}
    for result, status in query.values_list('result', 'status'):
        key = result or status or 'PENDING'
        verdicts[key] = verdicts.get(key, 0) + 1
    for name in query.values_list('language__name', flat=True):
        key = name or 'Language'
        languages[key] = languages.get(key, 0) + 1
    accepted = verdicts.get('AC', 0)
    sort_map = {
        'id': 'id',
        'user': 'user__user__username',
        'problem': 'problem__name',
        'language': 'language__name',
        'score': 'points',
        'runtime': 'time',
        'memory': 'memory',
        'created': 'date',
    }
    sort_key = sort_map.get(str(request.GET.get('sort') or 'id'), 'id')
    if str(request.GET.get('dir') or 'desc').lower() == 'desc':
        sort_key = '-' + sort_key
    page = max(1, int(request.GET.get('page') or 1))
    limit = max(1, min(100, int(request.GET.get('limit') or 20)))
    start = (page - 1) * limit
    rows = list(query.order_by(sort_key, '-id')[start:start + limit])
    return {
        'generatedAt': timezone.now().isoformat(),
        'rows': [_submission_row(submission) for submission in rows],
        'total': total,
        'page': page,
        'limit': limit,
        'summary': {
            'total': total,
            'accepted': accepted,
            'today': today,
            'verdicts': verdicts,
            'languages': languages,
        },
    }


def _current_profile(request):
    if not request.user.is_authenticated:
        return None
    profile, _ = Profile.objects.select_related('user').get_or_create(user=request.user)
    return profile


def _current_profile_from_user(user):
    if not user.is_authenticated:
        return None
    profile, _ = Profile.objects.select_related('user').get_or_create(user=user)
    return profile


def _active_contests_for_problem(problem):
    now = timezone.now()
    return Contest.objects.filter(
        contest_problems__problem=problem,
        start_time__lte=now,
    ).filter(
        Q(end_time__isnull=True) | Q(end_time__gte=now),
    ).distinct()


def _visible_contests_for_user(user):
    # CPPro management deliberately treats DMOJ staff as platform managers.
    # Everyone else must use the native private/organization contest filter.
    if _is_platform_admin(user):
        return Contest.objects.all()
    return Contest.get_visible_contests(user)


def _visible_submission_queryset(user):
    query = Submission.objects.all()
    if _is_platform_admin(user):
        return query

    visible_problems = Problem.get_visible_problems(user)
    profile = _current_profile_from_user(user)
    # The native contest visibility check only answers whether a contest can be
    # opened. Submission rows additionally require the native submission-list
    # rule, which prevents scoreboard-hidden and frozen contests from leaking
    # through this aggregate endpoint.
    contest_ids = [
        contest.id
        for contest in _visible_contests_for_user(user)
        if contest.can_see_full_submission_list(user)
    ]
    visibility = Q(contest_object__isnull=True)
    if contest_ids:
        visibility |= Q(contest_object_id__in=contest_ids)
    if profile:
        visibility |= Q(user=profile)
    return query.filter(problem__in=visible_problems).filter(visibility).distinct()


def _can_manage_problem(request_user, problem):
    if not request_user.is_authenticated:
        return False
    if request_user.is_staff or request_user.is_superuser:
        return True
    try:
        return bool(problem.is_editable_by(request_user))
    except Exception:
        profile = _current_profile_from_user(request_user)
        return bool(profile and (problem.authors.filter(pk=profile.id).exists() or problem.curators.filter(pk=profile.id).exists()))


def _can_view_submission_context(submission, request_user):
    contest = getattr(submission, 'contest_object', None)
    if contest is None:
        return True
    if not request_user.is_authenticated:
        return False
    profile = _current_profile_from_user(request_user)
    if (
        (profile and submission.user_id == profile.id)
        or request_user.has_perm('judge.view_all_submission')
        or _can_manage_problem(request_user, submission.problem)
    ):
        return True
    if not _visible_contests_for_user(request_user).filter(pk=contest.pk).exists():
        return False
    # Seeing a contest page is weaker than seeing other contestants' live
    # submissions. Honor the native frozen/hidden submission-list boundary for
    # direct IDs as well as aggregate endpoints.
    return bool(contest.can_see_full_submission_list(request_user))


def _can_view_submission_materials(submission, request_user, admin_context=False):
    # A problem manager needs testcase/source access to investigate live
    # submissions, even while a public frozen scoreboard stays sealed.
    if admin_context and _can_manage_problem(request_user, submission.problem):
        return True, 'admin'

    # Contest submissions and related test details remain sealed for everyone
    # else until every active contest containing this problem has ended.
    if _active_contests_for_problem(submission.problem).exists():
        return False, 'contest-restricted'

    profile = _current_profile_from_user(request_user)
    if request_user.is_staff or (profile and submission.user_id == profile.id):
        return True, 'owner-or-staff'
    return False, 'private'


def _json_error(message, status):
    return JsonResponse({'message': message}, status=status, json_dumps_params={'ensure_ascii': False})


def _read_json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except (TypeError, ValueError, UnicodeDecodeError):
        return None


def _logout_cookie_response():
    response = JsonResponse({'ok': True}, json_dumps_params={'ensure_ascii': False})
    for name in ['oj_platform_token', 'oj_platform_user', 'cppro_access_token', 'cppro_user']:
        response.delete_cookie(name, path='/')
    return response


@ensure_csrf_cookie
@require_GET
def cppro_auth_me(request):
    profile = _current_profile(request)
    if not profile:
        # The public frontend checks this endpoint on first render. Anonymous
        # browsing is a normal state, so return JSON success rather than a 401
        # that is surfaced as a failed network request in the browser console.
        return JsonResponse({'authenticated': False, 'user': None}, json_dumps_params={'ensure_ascii': False})
    return JsonResponse({'authenticated': True, 'user': _auth_user_row(profile)}, json_dumps_params={'ensure_ascii': False})


@csrf_protect
@require_http_methods(['POST'])
def cppro_auth_login(request):
    payload = _read_json_body(request)
    if payload is None:
        return _json_error('Invalid JSON body.', 400)
    username = str(payload.get('username') or '').strip()
    password = str(payload.get('password') or '')
    if not username or not password:
        return _json_error('Username and password are required.', 400)
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return _json_error('Tên đăng nhập hoặc mật khẩu không đúng.', 401)
    login(request, user)
    profile, _ = Profile.objects.select_related('user').get_or_create(user=user)
    return JsonResponse({'token': 'lcoj-session', 'user': _auth_user_row(profile)}, json_dumps_params={'ensure_ascii': False})


@csrf_protect
@require_http_methods(['POST'])
def cppro_auth_register(request):
    payload = _read_json_body(request)
    if payload is None:
        return _json_error('Invalid JSON body.', 400)
    username = str(payload.get('username') or '').strip()
    full_name = str(payload.get('fullName', payload.get('full_name')) or '').strip()
    email = str(payload.get('email') or '').strip()
    password = str(payload.get('password') or '')
    confirm_password = str(payload.get('confirmPassword', payload.get('confirm_password')) or '')
    if not confirm_password:
        confirm_password = password
    preferred_language = str(payload.get('favoriteLanguage', payload.get('favorite_language')) or '').strip()
    language = Language.objects.filter(key=preferred_language).first() or Language.get_default_language()
    form = CustomRegistrationForm(data={
        'username': username,
        'full_name': full_name,
        'email': email,
        'password1': password,
        'password2': confirm_password,
        'timezone': str(payload.get('timezone') or settings.DEFAULT_USER_TIME_ZONE),
        'language': str(language.pk),
        'organizations': [],
    })
    if not form.is_valid():
        messages = []
        for field_errors in form.errors.values():
            messages.extend(str(message) for message in field_errors)
        return _json_error(' '.join(messages) or 'Registration data is invalid.', 400)
    try:
        registration_view = RegistrationView()
        registration_view.setup(request)
        registration_view.register(form)
    except Exception:
        return _json_error('Không thể tạo tài khoản lúc này. Vui lòng thử lại sau.', 502)
    return JsonResponse({
        'created': True,
        'message': 'Tài khoản đã được tạo. Vui lòng mở email để kích hoạt trước khi đăng nhập.',
    }, status=201, json_dumps_params={'ensure_ascii': False})


@csrf_protect
@require_http_methods(['POST'])
def cppro_auth_logout(request):
    logout(request)
    return _logout_cookie_response()


@csrf_protect
@require_http_methods(['GET'])
def cppro_platform_settings(request):
    denied = _require_platform_admin(request)
    if denied:
        return denied
    return JsonResponse({
        'site': _normalize_platform_site_settings(request),
        'smtp': _smtp_public_settings(),
    }, json_dumps_params={'ensure_ascii': False})


@csrf_protect
@require_http_methods(['PUT'])
def cppro_platform_site_settings(request):
    denied = _require_platform_admin(request)
    if denied:
        return denied
    payload = _read_json_body(request)
    if payload is None:
        return _json_error('Invalid JSON body.', 400)
    try:
        site_settings = _normalize_platform_site_settings(request, payload)
    except (TypeError, ValueError):
        return _json_error('Website URLs must use HTTP(S) or a site-relative path.', 400)
    current_site = get_current_site(request)
    if hasattr(current_site, 'save'):
        current_site.name = site_settings['name'][:100]
        current_site.domain = site_settings['domain'][:100]
        current_site.save(update_fields=['name', 'domain'])
    _save_cppro_config_value(CPPRO_SITE_SETTINGS_KEY, site_settings)
    return JsonResponse({'site': site_settings}, json_dumps_params={'ensure_ascii': False})


@csrf_protect
@require_http_methods(['PUT'])
def cppro_platform_smtp_settings(request):
    denied = _require_platform_admin(request)
    if denied:
        return denied
    payload = _read_json_body(request)
    if payload is None:
        return _json_error('Invalid JSON body.', 400)
    current = _smtp_settings()
    try:
        port = int(payload.get('port', current['port']) or current['port'])
    except (TypeError, ValueError):
        return _json_error('SMTP port must be a number.', 400)
    if port < 1 or port > 65535:
        return _json_error('SMTP port must be between 1 and 65535.', 400)
    secure = bool(payload.get('secure'))
    start_tls = bool(payload.get('startTls', True))
    if secure and start_tls:
        return _json_error('Choose either SMTPS or STARTTLS, not both.', 400)
    password = current['password']
    if bool(payload.get('clearPassword')):
        password = ''
    elif payload.get('password'):
        password = str(payload.get('password'))
    next_settings = {
        'host': str(payload.get('host') or '').strip()[:255],
        'port': port,
        'secure': secure,
        'startTls': start_tls,
        'username': str(payload.get('username') or '').strip()[:255],
        'password': password,
        'from': str(payload.get('from') or '').strip()[:254],
        'ehloDomain': str(payload.get('ehloDomain') or 'localhost').strip()[:255] or 'localhost',
    }
    if next_settings['host'] and not next_settings['from']:
        return _json_error('A From email is required when SMTP is configured.', 400)
    _save_cppro_config_value(CPPRO_SMTP_SETTINGS_KEY, next_settings)
    return JsonResponse(_smtp_public_settings(), json_dumps_params={'ensure_ascii': False})


@csrf_protect
@require_http_methods(['POST'])
def cppro_platform_smtp_test(request):
    denied = _require_platform_admin(request)
    if denied:
        return denied
    smtp_settings = _smtp_settings()
    if not smtp_settings['host']:
        return _json_error('Save an SMTP host before running a connection test.', 400)
    connection = _smtp_connection(smtp_settings)
    try:
        connection.open()
    except Exception as error:
        return _json_error('SMTP connection test failed (%s).' % type(error).__name__, 502)
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return JsonResponse({
        'ok': True,
        'message': 'SMTP connection test completed.',
        'smtp': _smtp_public_settings(),
    }, json_dumps_params={'ensure_ascii': False})


def cppro_admin_smtp(request):
    """Staff-only SMTP configuration rendered inside the Django Admin shell."""
    current = _smtp_settings()
    smtp = _smtp_public_settings()
    draft = {
        'host': smtp['host'],
        'port': smtp['port'],
        'username': smtp['username'],
        'from': smtp['from'],
        'ehloDomain': smtp['ehloDomain'],
        'secure': smtp['secure'],
        'startTls': smtp['startTls'],
        'passwordConfigured': smtp['passwordConfigured'],
    }

    if request.method == 'POST':
        action = str(request.POST.get('action') or 'save').strip().lower()
        try:
            port = int(request.POST.get('port') or current['port'])
        except (TypeError, ValueError):
            messages.error(request, 'SMTP port must be a number.')
            port = current['port']
        draft.update({
            'host': str(request.POST.get('host') or '').strip()[:255],
            'port': port,
            'username': str(request.POST.get('username') or '').strip()[:255],
            'from': str(request.POST.get('from') or '').strip()[:254],
            'ehloDomain': str(request.POST.get('ehloDomain') or 'localhost').strip()[:255] or 'localhost',
            'secure': request.POST.get('secure') == 'on',
            'startTls': request.POST.get('startTls') == 'on',
            'passwordConfigured': bool(current['password']),
        })
        password = current['password']
        if request.POST.get('clearPassword') == 'on':
            password = ''
        elif request.POST.get('password'):
            password = str(request.POST.get('password'))
        next_settings = {
            'host': draft['host'],
            'port': draft['port'],
            'secure': draft['secure'],
            'startTls': draft['startTls'],
            'username': draft['username'],
            'password': password,
            'from': draft['from'],
            'ehloDomain': draft['ehloDomain'],
        }

        error = None
        if port < 1 or port > 65535:
            error = 'SMTP port must be between 1 and 65535.'
        elif next_settings['secure'] and next_settings['startTls']:
            error = 'Choose either SMTPS or STARTTLS, not both.'
        elif next_settings['host'] and not next_settings['from']:
            error = 'A From email is required when SMTP is configured.'
        if error:
            messages.error(request, error)
        elif action == 'test':
            if not next_settings['host']:
                messages.error(request, 'Enter an SMTP host before running a connection test.')
            else:
                connection = _smtp_connection(next_settings)
                try:
                    connection.open()
                except Exception as error:
                    messages.error(request, 'SMTP connection test failed (%s).' % type(error).__name__)
                else:
                    messages.success(request, 'SMTP connection test completed.')
                finally:
                    try:
                        connection.close()
                    except Exception:
                        pass
        else:
            _save_cppro_config_value(CPPRO_SMTP_SETTINGS_KEY, next_settings)
            messages.success(request, 'SMTP settings saved. Django email now uses this configuration.')
            return redirect('cppro_admin_smtp')

    return TemplateResponse(request, 'admin/cppro_smtp.html', {
        **admin.site.each_context(request),
        'title': 'SMTP configuration',
        'smtp': draft,
    })


def _ensure_problem_has_judge(problem):
    if problem.judges.exists():
        return
    Judge = apps.get_model('judge', 'Judge')
    judges = list(Judge.objects.filter(is_disabled=False).order_by('id'))
    if judges:
        problem.judges.add(*judges)


@require_GET
def cppro_data(request):
    profile = _current_profile(request)
    visible_submissions = _visible_submission_queryset(request.user)
    visible = Problem.get_visible_problems(request.user)
    problems = list(
        visible.select_related('group')
        .prefetch_related('types', 'allowed_languages', 'authors__user')
        .order_by('code')
    )
    submission_counts = {}
    for problem_id in visible_submissions.filter(problem_id__in=[p.id for p in problems]).values_list('problem_id', flat=True):
        submission_counts[problem_id] = submission_counts.get(problem_id, 0) + 1
    contests = list(
        _visible_contests_for_user(request.user)
        .prefetch_related('contest_problems__problem')
        .order_by('-start_time', 'key')
    )
    participation_counts, virtual_participation_counts = _contest_participation_counts([contest.id for contest in contests])
    organizations = [
        organization
        for organization in Organization.objects.prefetch_related('admins__user', 'members__user').order_by('name')
        if _can_view_organization(organization, profile, request.user)
    ]
    organization_problem_counts, organization_contest_counts = _organization_counts(organizations, request.user)
    user_progress = {}
    if profile:
        for submission in (
            Submission.objects
            .filter(user=profile, problem_id__in=[problem.id for problem in problems])
            .order_by('-date', '-id')
            .values('problem_id', 'result', 'status', 'points', 'case_points', 'case_total')
        ):
            problem_id = submission['problem_id']
            progress = user_progress.setdefault(problem_id, {'attempts': 0, 'solved': False, 'latest_verdict': '', 'best_verdict': ''})
            progress['attempts'] += 1
            verdict = submission.get('result') or submission.get('status') or ''
            if not progress['latest_verdict']:
                progress['latest_verdict'] = verdict
            if verdict and not progress['best_verdict']:
                progress['best_verdict'] = verdict
            case_total = float(submission.get('case_total') or 0)
            case_points = float(submission.get('case_points') or 0)
            fully_accepted = verdict == 'AC' and (case_total <= 0 or case_points >= case_total)
            if fully_accepted:
                progress['solved'] = True
                progress['best_verdict'] = 'AC'

    submissions = list(
        visible_submissions
        .select_related('problem', 'language', 'user__user', 'contest_object')
        .order_by('-date', '-id')[:200]
    )
    visible_submission_counts = dict(
        visible_submissions
        .values('user_id')
        .annotate(total=Count('id'))
        .values_list('user_id', 'total')
    )

    runtime_by_language = {
        key: name
        for key, name in RuntimeVersion.objects.select_related('language').values_list('language__key', 'name')
    }
    languages = [
        {
            'code': language.key,
            'label': language.name,
            'runtime_label': runtime_by_language.get(language.key) or '',
            'source_template': language.template or '',
        }
        for language in Language.objects.order_by('key')
    ]
    users = [
        _public_profile_row(profile, visible_submission_counts.get(profile.id, 0))
        for profile in (
            Profile.objects
            .select_related('user')
            .prefetch_related(
                'badges',
                Prefetch(
                    'organizations',
                    queryset=Organization.objects.filter(is_unlisted=False),
                    to_attr='public_organizations',
                ),
            )
            .order_by('user__username')
        )
        if not profile.is_unlisted and profile.user.is_active
    ]
    users.sort(key=lambda row: (-float(row.get('pp_score') or 0), -float(row.get('score') or 0), -int(row.get('solved') or 0), row['username'].lower()))
    problem_rows = [_problem_row(problem, submission_counts, user_progress) for problem in problems]
    contest_rows = [
        _contest_row(
            contest,
            participation_counts,
            virtual_participation_counts,
            submission_counts,
            profile,
            request.user,
        )
        for contest in contests
    ]
    blog_posts = _visible_registered_posts('community', request.user)
    post_rows = [_post_row(post) for post in blog_posts]
    notification_post_rows = [
        _post_row(post, 'announcement')
        for post in _visible_registered_posts('announcement', request.user)
    ]
    announcement_rows = [
        _contest_announcement_row(announcement)
        for announcement in (
            ContestAnnouncement.objects
            .select_related('contest')
            .filter(contest_id__in=[contest.id for contest in contests])
            .order_by('-date', '-id')[:24]
        )
    ]
    try:
        Quiz = apps.get_model('quiz', 'Quiz')
        quizzes = list(
            Quiz.objects
            .filter(is_public=True)
            .annotate(question_count=Count('question_links', distinct=True), total_points=Sum('question_links__points'))
            .order_by('start_time', 'name', 'id')[:60]
        )
        quiz_rows = [_quiz_row(quiz) for quiz in quizzes]
    except LookupError:
        quiz_rows = []
    traffic = _home_traffic_snapshot()
    accepted_submissions = visible_submissions.filter(result='AC').count()
    total_submissions = visible_submissions.count()
    challenge = problem_rows[timezone.localdate().toordinal() % len(problem_rows)] if problem_rows else None
    payload = {
        'generatedAt': timezone.now().isoformat(),
        'source': 'lcoj-database',
        'stats': {
            'problems': len(problems),
            'submissions': total_submissions,
            'contests': len(contests),
            'organizations': len(organizations),
            'languages': len(languages),
            'users': len(users),
            'quizzes': len(quiz_rows),
            'acceptanceRate': round((accepted_submissions / total_submissions) * 100, 1) if total_submissions else 0,
            'onlineUsers': len(traffic['presence']),
            'visits': traffic['visits'],
            'pageViews': traffic['pageViews'],
        },
        'problems': problem_rows,
        'contests': contest_rows,
        'contestDetails': {
            contest.key: _contest_row(
                contest,
                participation_counts,
                virtual_participation_counts,
                submission_counts,
                profile,
                request.user,
            )
            for contest in contests
        },
        'organizations': [
            _organization_row(
                organization,
                profile,
                organization_problem_counts,
                organization_contest_counts,
                include_unlisted_admins=_can_manage_organization(organization, profile, request.user),
            )
            for organization in organizations
        ],
        'submissions': [_submission_row(submission) for submission in submissions],
        'users': users,
        'profiles': {user['username']: user for user in users},
        'languages': languages,
        'posts': post_rows,
        'notifications': announcement_rows + notification_post_rows,
        'quizzes': quiz_rows,
        'homeSummary': {
            'activity': _home_activity_rows(visible_submissions),
            'streak': {'current': 0, 'longest': 0},
            'challenge': challenge,
            'presence': {'online': len(traffic['presence']), 'total': len(users)},
            'traffic': {'visits': traffic['visits'], 'pageViews': traffic['pageViews']},
        },
        'siteSettings': {
            'site': _normalize_platform_site_settings(request),
        },
    }
    return JsonResponse(payload, json_dumps_params={'ensure_ascii': False})


@csrf_protect
@require_http_methods(['POST'])
def cppro_home_visit(request):
    body = _read_json_body(request)
    if body is None:
        return _json_error('Invalid JSON body.', 400)
    presence_id = str(body.get('presenceId') or '').strip()
    if presence_id and (len(presence_id) > 96 or not re.match(r'^[A-Za-z0-9_-]+$', presence_id)):
        return _json_error('Invalid presence identifier.', 400)
    with transaction.atomic():
        row = MiscConfig.objects.select_for_update().filter(key=CPPRO_HOME_TRAFFIC_KEY).order_by('id').first()
        try:
            config = json.loads(row.value) if row and row.value else {}
        except (TypeError, ValueError):
            config = {}
        snapshot = _home_traffic_snapshot(config)
        snapshot['pageViews'] += 1
        if body.get('visit') is not False:
            snapshot['visits'] += 1
        if presence_id:
            snapshot['presence'][presence_id] = timezone.now().timestamp()
        saved = {
            'visits': snapshot['visits'],
            'pageViews': snapshot['pageViews'],
            'presence': snapshot['presence'],
        }
        encoded = json.dumps(saved, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        if row:
            row.value = encoded
            row.save(update_fields=['value'])
        else:
            MiscConfig.objects.create(key=CPPRO_HOME_TRAFFIC_KEY, value=encoded)
    return JsonResponse({
        'visits': snapshot['visits'],
        'pageViews': snapshot['pageViews'],
        'presence': {'online': len(snapshot['presence'])},
    }, json_dumps_params={'ensure_ascii': False})


def _find_visible_contest(identifier, request_user):
    query = (
        _visible_contests_for_user(request_user)
        .prefetch_related('contest_problems__problem')
    )
    contest = None
    contest_problem = None
    participation = None
    if str(identifier).isdigit():
        contest = query.filter(id=int(identifier)).first()
    if contest is None:
        contest = query.filter(key=str(identifier)).first()
    return contest


def _contest_payload(request, contest):
    profile = _current_profile(request)
    submission_counts = {}
    problem_ids = list(contest.contest_problems.values_list('problem_id', flat=True))
    for problem_id in _visible_submission_queryset(request.user).filter(problem_id__in=problem_ids).values_list('problem_id', flat=True):
        submission_counts[problem_id] = submission_counts.get(problem_id, 0) + 1
    participation_counts, virtual_participation_counts = _contest_participation_counts([contest.id])
    can_view_full, _, frozen = _contest_scoreboard_access(contest, request.user)
    row = _contest_row(
        contest,
        participation_counts,
        virtual_participation_counts,
        submission_counts,
        profile,
        request.user,
    )
    row['participant_users'] = []
    row['participant_users_truncated'] = False
    row['participant_users_available'] = bool(can_view_full)
    if can_view_full:
        participant_rows = list(
            ContestParticipation.objects
            .filter(contest=contest, virtual__gte=ContestParticipation.LIVE, user__user__is_active=True)
            .select_related('user__user')
            .order_by('virtual', 'real_start', 'id')[:250]
        )
        row['participant_users'] = [
            _contest_participant_row(participation, frozen=frozen)
            for participation in participant_rows
        ]
        row['participant_users_truncated'] = row['participant_total'] > len(participant_rows)
    return {'contest': row, **row}


def _parse_management_datetime(value, fallback):
    if value in (None, ''):
        return fallback
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise ValueError('Contest date/time must use ISO-8601 format.')
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _admin_contest(identifier):
    query = Contest.objects.prefetch_related('contest_problems__problem')
    contest = query.filter(id=int(identifier)).first() if str(identifier).isdigit() else None
    if contest is None:
        contest = query.filter(key=str(identifier)).first()
    return contest


def _apply_contest_status(contest, requested_status):
    status = str(requested_status or '').strip().lower()
    now = timezone.now()
    if status == 'draft':
        contest.is_visible = False
    elif status in {'upcoming', 'scheduled'}:
        contest.is_visible = True
        if contest.start_time <= now:
            contest.start_time = now + timedelta(hours=1)
        if contest.end_time <= contest.start_time:
            contest.end_time = contest.start_time + timedelta(hours=2)
    elif status in {'live', 'running'}:
        contest.is_visible = True
        if contest.start_time > now:
            contest.start_time = now - timedelta(minutes=1)
        if contest.end_time <= now:
            contest.end_time = now + timedelta(hours=2)
    elif status in {'finished', 'ended'}:
        contest.is_visible = True
        if contest.start_time > now:
            contest.start_time = now - timedelta(hours=2)
        contest.end_time = min(contest.end_time, now - timedelta(seconds=1))


def _set_contest_problems(contest, raw_ids):
    if raw_ids is None:
        return
    if not isinstance(raw_ids, list):
        raise ValueError('problemIds must be a list.')
    identifiers = [int(item) for item in raw_ids if str(item).isdigit()]
    problems = list(Problem.objects.filter(id__in=identifiers))
    if len(problems) != len(set(identifiers)):
        raise ValueError('One or more selected problems do not exist.')
    by_id = {problem.id: problem for problem in problems}
    ContestProblem.objects.filter(contest=contest).delete()
    ContestProblem.objects.bulk_create([
        ContestProblem(
            contest=contest,
            problem=by_id[problem_id],
            points=max(0, int(round(float(by_id[problem_id].points or 100)))),
            order=index,
        )
        for index, problem_id in enumerate(identifiers, start=1)
    ])


CPPRO_CONTEST_FORMAT_ALIASES = {
    'contest': 'default',
    'standard': 'default',
    'default': 'default',
    'atcoder': 'atcoder',
    'ecoo': 'ecoo',
    'icpc': 'icpc',
    'ioi': 'ioi16',
    'ioi16': 'ioi16',
    'ioi-2016': 'ioi16',
    'ioi_legacy': 'ioi',
    'legacy-ioi': 'ioi',
    'legacy_ioi': 'ioi',
    'vnoj': 'vnoj',
}


def _normalize_contest_format(value, fallback='default'):
    raw = str(value or fallback or 'default').strip().lower().replace(' ', '-').replace('/', '-')
    normalized = CPPRO_CONTEST_FORMAT_ALIASES.get(raw, raw)
    allowed = {key for key, _label in (Contest._meta.get_field('format_name').choices or [])}
    if normalized not in allowed:
        raise ValueError('Unknown contest format. Choose Default, ICPC, IOI, AtCoder, ECOO, or VNOJ.')
    return normalized


def _contest_scoring_config(format_name, payload, existing=None):
    raw = payload.get('scoringConfig', payload.get('scoring_config'))
    if raw is None:
        format_payload = payload.get('formatConfig', payload.get('format_config'))
        if isinstance(format_payload, dict):
            scoring_keys = {
                'penalty', 'penaltyMinutes', 'cumtime', 'first_ac_bonus', 'firstAcBonus',
                'time_bonus', 'timeBonus', 'last_score_altering', 'lastScoreAltering', 'LSO', 'lso',
            }
            raw = {key: value for key, value in format_payload.items() if key in scoring_keys}
    if raw is None and 'format' not in payload:
        return dict(existing or {}) if isinstance(existing, dict) else {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError('Contest scoring configuration must be an object.')

    def integer_value(*keys, minimum=0, maximum=10000):
        value = next((raw.get(key) for key in keys if key in raw), None)
        if value in (None, ''):
            return None
        number = int(value)
        if number < minimum or number > maximum:
            raise ValueError('Contest scoring value must be between %s and %s.' % (minimum, maximum))
        return number

    config = {}
    if format_name in {'icpc', 'atcoder', 'vnoj'}:
        penalty = integer_value('penalty', 'penaltyMinutes', minimum=0, maximum=360)
        if penalty is not None:
            config['penalty'] = penalty
    if format_name in {'ecoo', 'ioi', 'ioi16'} and 'cumtime' in raw:
        config['cumtime'] = _admin_boolean(raw.get('cumtime'))
    if format_name == 'ecoo':
        first_ac_bonus = integer_value('first_ac_bonus', 'firstAcBonus')
        time_bonus = integer_value('time_bonus', 'timeBonus')
        if first_ac_bonus is not None:
            config['first_ac_bonus'] = first_ac_bonus
        if time_bonus is not None:
            config['time_bonus'] = time_bonus
    if format_name == 'ioi' and ('last_score_altering' in raw or 'lastScoreAltering' in raw):
        config['last_score_altering'] = _admin_boolean(raw.get('last_score_altering', raw.get('lastScoreAltering')))
    if format_name == 'vnoj' and ('LSO' in raw or 'lso' in raw):
        config['LSO'] = _admin_boolean(raw.get('LSO', raw.get('lso')))
    return config


def _apply_contest_payload(contest, payload):
    title = str(payload.get('title') or contest.name).strip()
    if len(title) < 3:
        raise ValueError('Contest title must contain at least three characters.')
    contest.name = title[:100]
    if 'description' in payload:
        contest.description = str(payload.get('description') or '')
    contest.start_time = _parse_management_datetime(payload.get('startTime', payload.get('start_time')), contest.start_time)
    contest.end_time = _parse_management_datetime(payload.get('endTime', payload.get('end_time')), contest.end_time)
    if contest.end_time <= contest.start_time:
        raise ValueError('Contest end time must be after its start time.')
    if 'visibility' in payload:
        visibility = str(payload.get('visibility') or 'public').lower()
        contest.is_private = visibility == 'private'
        contest.is_organization_private = visibility == 'organization'
    if 'allowVirtual' in payload or 'allow_virtual' in payload:
        contest.disallow_virtual = not _admin_boolean(payload.get('allowVirtual', payload.get('allow_virtual')), True)
    if 'isRated' in payload or 'is_rated' in payload:
        contest.is_rated = _admin_boolean(payload.get('isRated', payload.get('is_rated')))
    contest.format_name = _normalize_contest_format(payload.get('format'), contest.format_name)
    contest.format_config = _contest_scoring_config(contest.format_name, payload, contest.format_config) or None
    if 'freezeMinutes' in payload or 'freeze_minutes' in payload:
        freeze_minutes = int(payload.get('freezeMinutes', payload.get('freeze_minutes')) or 0)
        duration_minutes = int((contest.end_time - contest.start_time).total_seconds() // 60)
        if freeze_minutes < 0 or freeze_minutes > duration_minutes:
            raise ValueError('Freeze duration must be between 0 and the contest duration.')
        if freeze_minutes and contest.format_name not in {'icpc', 'vnoj'}:
            raise ValueError('Leaderboard freeze is supported by LCOJ for ICPC and VNOJ formats.')
        contest.frozen_last_minutes = freeze_minutes
    if 'scoreboardVisibility' in payload or 'scoreboard_visibility' in payload:
        visibility = str(payload.get('scoreboardVisibility', payload.get('scoreboard_visibility')) or 'V').upper()
        allowed_visibility = {key for key, _label in (Contest._meta.get_field('scoreboard_visibility').choices or [])}
        if visibility not in allowed_visibility:
            raise ValueError('Unknown scoreboard visibility policy.')
        contest.scoreboard_visibility = visibility
    if 'showSubmissionList' in payload or 'show_submission_list' in payload:
        contest.show_submission_list = _admin_boolean(payload.get('showSubmissionList', payload.get('show_submission_list')))
    _apply_contest_status(contest, payload.get('status'))
    contest.__dict__.pop('format_class', None)
    contest.__dict__.pop('format', None)
    try:
        contest.format_class.validate(contest.format_config)
    except ValidationError as error:
        raise ValueError('; '.join(error.messages)) from error
    contest.save()
    _set_contest_problems(contest, payload.get('problemIds', payload.get('problem_ids')))
    format_config = payload.get('formatConfig', payload.get('format_config'))
    if isinstance(format_config, dict):
        languages = format_config.get('allowedLanguages', format_config.get('allowed_languages', []))
        if not isinstance(languages, list):
            raise ValueError('Contest allowed languages must be a list.')
        keys = [str(item) for item in languages]
        if Language.objects.filter(key__in=keys).count() != len(set(keys)):
            raise ValueError('One or more selected contest languages do not exist.')
        _save_cppro_config_value('cppro_contest_%s' % contest.id, {'allowedLanguages': keys})


@csrf_protect
@require_http_methods(['GET', 'POST'])
def cppro_contests(request):
    if request.method == 'GET':
        query = _visible_contests_for_user(request.user).prefetch_related('contest_problems__problem').order_by('-start_time', 'key')
        contests = list(query[:500])
        participation_counts, virtual_counts = _contest_participation_counts([item.id for item in contests])
        submission_counts = {}
        for problem_id in _visible_submission_queryset(request.user).filter(
            contest_object_id__in=[item.id for item in contests],
        ).values_list('problem_id', flat=True):
            submission_counts[problem_id] = submission_counts.get(problem_id, 0) + 1
        rows = [
            _contest_row(item, participation_counts, virtual_counts, submission_counts, _current_profile(request), request.user)
            for item in contests
        ]
        return JsonResponse({'rows': rows, 'total': query.count()}, json_dumps_params={'ensure_ascii': False})
    denied = _require_platform_admin(request)
    if denied:
        return denied
    payload = _admin_payload(request)
    title = str(payload.get('title') or '').strip()
    if len(title) < 3:
        return _json_error('Contest title must contain at least three characters.', 400)
    key = _unique_model_code(Contest, payload.get('externalId'), title, field='key')
    start_time = timezone.now() + timedelta(days=1)
    end_time = start_time + timedelta(hours=2)
    try:
        contest = Contest.objects.create(
            key=key,
            name=title[:100],
            description=str(payload.get('description') or ''),
            start_time=start_time,
            end_time=end_time,
            is_visible=False,
            format_name='default',
        )
        _apply_contest_payload(contest, payload)
    except (TypeError, ValueError) as error:
        Contest.objects.filter(key=key).delete()
        return _json_error(str(error), 400)
    return JsonResponse(_contest_payload(request, contest), status=201, json_dumps_params={'ensure_ascii': False})


@csrf_protect
@require_http_methods(['GET', 'PATCH', 'PUT', 'DELETE'])
def cppro_contest_detail(request, identifier):
    if request.method == 'GET':
        contest = _find_visible_contest(identifier, request.user)
        if not contest:
            return _json_error('Contest not found.', 404)
        return JsonResponse(_contest_payload(request, contest), json_dumps_params={'ensure_ascii': False})
    denied = _require_platform_admin(request)
    if denied:
        return denied
    contest = _admin_contest(identifier)
    if not contest:
        return _json_error('Contest not found.', 404)
    if request.method == 'DELETE':
        deleted = {'id': contest.id, 'key': contest.key}
        contest.delete()
        return JsonResponse({'ok': True, 'deleted': deleted})
    payload = _admin_payload(request)
    try:
        with transaction.atomic():
            _apply_contest_payload(contest, payload)
    except (TypeError, ValueError) as error:
        return _json_error(str(error), 400)
    contest = _admin_contest(contest.id)
    return JsonResponse(_contest_payload(request, contest), json_dumps_params={'ensure_ascii': False})


def _positive_query_integer(request, name, default, maximum):
    try:
        value = int(request.GET.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, 1), maximum)


def _contest_problem_label(contest, index):
    """Use alphabetic labels when a contest format supplies numeric defaults."""
    try:
        label = str(contest.get_label_for_problem(index) or '').strip()
    except Exception:
        label = ''
    if label and not label.isdigit():
        return label

    number = index + 1
    letters = []
    while number:
        number, remainder = divmod(number - 1, 26)
        letters.append(chr(ord('A') + remainder))
    return ''.join(reversed(letters))


def _contest_submission_score(submission, contest_problem):
    """Translate a problem score to the score allocated in its contest."""
    try:
        earned = max(0.0, float(submission.points or 0))
        problem_max = max(0.0, float(contest_problem.problem.points or 0))
        contest_max = max(0.0, float(contest_problem.points or 0))
    except (AttributeError, TypeError, ValueError):
        return 0.0
    if contest_max <= 0:
        return 0.0
    if problem_max > 0:
        return min(contest_max, earned * contest_max / problem_max)
    return min(contest_max, earned)


def _contest_submission_elapsed_seconds(participation, submitted_at):
    try:
        return max(0, int((submitted_at - participation.start).total_seconds()))
    except (AttributeError, TypeError, ValueError):
        return 0


def _contest_submission_in_participation_window(participation, submitted_at):
    if not submitted_at:
        return False
    try:
        if submitted_at < participation.start:
            return False
        end_time = participation.end_time
        return end_time is None or submitted_at <= end_time
    except (AttributeError, TypeError, ValueError):
        return False


def _record_contest_problem_result(problem_results, participation, contest_problem, submission, points):
    """Keep the best score and the earliest time that achieved that score."""
    participation_results = problem_results.setdefault(participation.id, {})
    previous = participation_results.get(contest_problem.id)
    contest_max = max(0.0, float(contest_problem.points or 0))
    score = min(contest_max, max(0.0, float(points or 0))) if contest_max else 0.0
    elapsed_seconds = _contest_submission_elapsed_seconds(participation, submission.date)
    accepted = bool(
        submission.result == 'AC'
        or (contest_max > 0 and score >= contest_max)
    )
    candidate = {
        'score': score,
        'attempts': (previous or {}).get('attempts', 0) + 1,
        'elapsed_seconds': elapsed_seconds,
        'submission_id': submission.id,
        'accepted': accepted,
    }
    if previous:
        is_better = candidate['score'] > previous['score']
        is_equal_but_earlier = (
            candidate['score'] == previous['score']
            and (
                (candidate['accepted'] and not previous.get('accepted'))
                or candidate['elapsed_seconds'] < previous['elapsed_seconds']
            )
        )
        if not is_better and not is_equal_but_earlier:
            previous['attempts'] = candidate['attempts']
            return
    participation_results[contest_problem.id] = candidate


@require_GET
def cppro_contest_standings(request, identifier):
    contest = _find_visible_contest(identifier, request.user)
    if not contest:
        return _json_error('Contest not found.', 404)
    if not (_is_platform_admin(request.user) or contest.can_see_own_scoreboard(request.user)):
        return _json_error('Contest scoreboard is not available.', 403)

    profile = _current_profile(request)
    can_view_full, can_edit, frozen = _contest_scoreboard_access(contest, request.user)

    participations = (
        ContestParticipation.objects
        .filter(contest=contest, virtual=ContestParticipation.LIVE)
        .select_related('contest', 'user__user')
        .prefetch_related(
            Prefetch(
                'user__organizations',
                queryset=Organization.objects.filter(is_unlisted=False),
                to_attr='public_organizations',
            ),
        )
        .annotate(submission_count=Count('submission'))
    )
    if not can_view_full:
        if profile is None:
            return _json_error('Authentication required.', 401)
        participations = participations.filter(user=profile)
    if frozen:
        participations = participations.order_by(
            'is_disqualified', '-frozen_score', 'frozen_cumtime',
            'frozen_tiebreaker', '-submission_count', 'id',
        )
    else:
        participations = participations.order_by(
            'is_disqualified', '-score', 'cumtime',
            'tiebreaker', '-submission_count', 'id',
        )

    participation_rows = list(participations)
    participation_ids = [item.id for item in participation_rows]
    contest_problems = list(contest.contest_problems.select_related('problem').order_by('order', 'id'))
    contest_problem_by_problem_id = {item.problem_id: item for item in contest_problems}
    problem_columns = []
    for index, contest_problem in enumerate(contest_problems):
        problem_columns.append({
            'id': contest_problem.id,
            'problem_id': contest_problem.problem_id,
            'code': contest_problem.problem.code,
            'title': contest_problem.problem.name,
            'label': _contest_problem_label(contest, index),
            'points': float(contest_problem.points or 0),
            'order': int(contest_problem.order or index + 1),
        })
    problem_results = {participation_id: {} for participation_id in participation_ids}
    if participation_ids and (not frozen or can_edit):
        participation_by_id = {item.id: item for item in participation_rows}
        participations_by_user = {}
        for participation in participation_rows:
            participations_by_user.setdefault(participation.user_id, []).append(participation)
        seen_submission_ids = set()
        for contest_submission in (
            ContestSubmission.objects
            .filter(participation_id__in=participation_ids, problem__contest=contest)
            .select_related('submission', 'problem', 'participation')
            .order_by('submission__date', 'id')
        ):
            participation = participation_by_id.get(contest_submission.participation_id)
            if participation is None or not _contest_submission_in_participation_window(participation, contest_submission.submission.date):
                continue
            seen_submission_ids.add(contest_submission.submission_id)
            stored_score = max(0.0, float(contest_submission.points or 0))
            source_score = _contest_submission_score(contest_submission.submission, contest_submission.problem)
            _record_contest_problem_result(
                problem_results,
                participation,
                contest_submission.problem,
                contest_submission.submission,
                max(stored_score, source_score),
            )

        start_times = [item.start for item in participation_rows if item.start]
        end_times = [item.end_time for item in participation_rows if item.end_time]
        fallback_submissions = Submission.objects.filter(
            user_id__in=list(participations_by_user),
            problem_id__in=list(contest_problem_by_problem_id),
        ).filter(
            Q(contest_object=contest) | Q(contest_object__isnull=True),
        ).select_related('problem').order_by('date', 'id')
        if start_times:
            fallback_submissions = fallback_submissions.filter(date__gte=min(start_times))
        if end_times:
            fallback_submissions = fallback_submissions.filter(date__lte=max(end_times))
        for submission in fallback_submissions:
            if submission.id in seen_submission_ids:
                continue
            candidates = participations_by_user.get(submission.user_id, [])
            if submission.contest_object_id is None:
                candidates = [item for item in candidates if item.virtual == ContestParticipation.LIVE]
            candidates = [
                item for item in candidates
                if _contest_submission_in_participation_window(item, submission.date)
            ]
            if not candidates:
                continue
            candidates.sort(
                key=lambda item: (
                    item.real_start or datetime(1970, 1, 1, tzinfo=datetime_timezone.utc),
                    item.id,
                ),
                reverse=True,
            )
            contest_problem = contest_problem_by_problem_id.get(submission.problem_id)
            if contest_problem is None:
                continue
            _record_contest_problem_result(
                problem_results,
                candidates[0],
                contest_problem,
                submission,
                _contest_submission_score(submission, contest_problem),
            )

    summaries = {}
    if not frozen or can_edit:
        precision = max(0, int(getattr(contest, 'points_precision', 0) or 0))
        for participation in participation_rows:
            results = problem_results.get(participation.id, {})
            score = round(sum(float(item.get('score') or 0) for item in results.values()), precision)
            solved = sum(1 for item in results.values() if item.get('accepted'))
            penalty = sum(int(item.get('elapsed_seconds') or 0) for item in results.values() if item.get('accepted'))
            summaries[participation.id] = {
                'score': score,
                'solved': solved,
                'penalty': penalty,
                'submissions': sum(int(item.get('attempts') or 0) for item in results.values()),
            }
        participation_rows.sort(key=lambda item: (
            bool(item.is_disqualified),
            -summaries[item.id]['score'],
            summaries[item.id]['penalty'],
            -summaries[item.id]['solved'],
            item.id,
        ))

    rows = []
    last_rank_key = None
    visible_rank = 0
    for index, participation in enumerate(participation_rows, start=1):
        summary = summaries.get(participation.id)
        if summary is not None:
            score = summary['score']
            cumtime = summary['penalty']
            tiebreaker = 0.0
            solved = summary['solved']
            submission_count = summary['submissions']
        else:
            score = float(participation.frozen_score if frozen else participation.score or 0)
            cumtime = int(participation.frozen_cumtime if frozen else participation.cumtime or 0)
            tiebreaker = float(participation.frozen_tiebreaker if frozen else participation.tiebreaker or 0)
            solved = 0
            submission_count = int(participation.submission_count or 0)
        rank_key = (bool(participation.is_disqualified), score, cumtime, solved, tiebreaker)
        if rank_key != last_rank_key:
            visible_rank = index
            last_rank_key = rank_key
        user = participation.user.user
        organization = _profile_organization(participation.user)
        rows.append({
            'rank': visible_rank,
            'participation_id': participation.id,
            'user_id': participation.user_id,
            'username': user.username,
            'full_name': user.get_full_name() or participation.user.display_name or user.username,
            'score': score,
            'solved': solved,
            'penalty': cumtime,
            'cumtime': cumtime,
            'tiebreaker': tiebreaker,
            'submissions': submission_count,
            'organization_name': getattr(organization, 'name', '') if organization else '',
            'virtual': bool(participation.virtual),
            'problem_results': [
                problem_results.get(participation.id, {}).get(column['id'])
                for column in problem_columns
            ] if (not frozen or can_edit) else [],
            'disqualified': bool(participation.is_disqualified),
            'frozen': frozen,
        })

    page = _positive_query_integer(request, 'page', 1, 100000)
    limit = _positive_query_integer(request, 'limit', 100, 500)
    total = len(rows)
    start = (page - 1) * limit
    return JsonResponse({
        'rows': rows[start:start + limit],
        'total': total,
        'page': page,
        'limit': limit,
        'contest': contest.key,
        'problems': problem_columns,
        'show_problem_scores': bool(not frozen or can_edit),
        'frozen': frozen,
        'full_scoreboard': can_view_full,
    }, json_dumps_params={'ensure_ascii': False})


def _parse_json_body(request):
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _access_code_from_payload(payload):
    return str(payload.get('accessCode') or payload.get('access_code') or '').strip()


@csrf_protect
@require_http_methods(['POST'])
def cppro_contest_join(request, identifier):
    profile = _current_profile(request)
    if not profile:
        return _json_error('Authentication required.', 401)

    contest = _find_visible_contest(identifier, request.user)
    if not contest:
        return _json_error('Contest not found.', 404)

    payload = _parse_json_body(request)
    access_code = _access_code_from_payload(payload)
    can_edit = bool(request.user.is_superuser or contest.is_editable_by(request.user))
    requires_access_code = bool(not can_edit and contest.access_code and access_code != contest.access_code)

    if not request.user.is_superuser and contest.banned_users.filter(id=profile.id).exists():
        return _json_error('You are banned from joining this contest.', 403)

    if profile.current_contest and profile.current_contest.contest_id != contest.id:
        if payload.get('leaveOtherContests', True):
            profile.remove_contest()
        else:
            return _json_error('You are already participating in another contest.', 409)

    if requires_access_code:
        return _json_error('Contest access code is required.', 403)

    participation = None
    if contest.ended:
        if contest.disallow_virtual:
            return _json_error('Virtual joining is not allowed for this contest.', 400)
        while True:
            virtual_id = max(
                (ContestParticipation.objects.filter(contest=contest, user=profile).aggregate(virtual_id=Max('virtual'))['virtual_id'] or 0) + 1,
                1,
            )
            try:
                participation = ContestParticipation.objects.create(
                    contest=contest,
                    user=profile,
                    virtual=virtual_id,
                    real_start=timezone.now(),
                )
            except IntegrityError:
                continue
            break
    elif not contest.can_join:
        if contest.require_registration and contest.can_register:
            participation, created = ContestParticipation.objects.get_or_create(
                contest=contest,
                user=profile,
                virtual=ContestParticipation.LIVE,
                defaults={'real_start': datetime(1970, 1, 1, tzinfo=datetime_timezone.utc)},
            )
            if not created and not participation.pre_registered:
                profile.current_contest = participation
                profile.save(update_fields=['current_contest'])
        else:
            return _json_error('Contest is not currently open for joining.', 400)
    else:
        existing_live = ContestParticipation.objects.filter(
            contest=contest,
            user=profile,
            virtual=ContestParticipation.LIVE,
        ).first()
        if contest.require_registration and not contest.can_register and existing_live is None:
            return _json_error('Contest registration is required before joining.', 403)
        participation, _ = ContestParticipation.objects.get_or_create(
            contest=contest,
            user=profile,
            virtual=ContestParticipation.LIVE,
            defaults={'real_start': timezone.now()},
        )
        if participation.pre_registered:
            participation.real_start = timezone.now()
            participation.save(update_fields=['real_start'])

    if participation and not participation.pre_registered:
        profile.current_contest = participation
        profile.save(update_fields=['current_contest'])

    contest._updating_stats_only = True
    contest.update_user_count()
    contest = _find_visible_contest(identifier, request.user) or contest
    response = _contest_payload(request, contest)
    response['joined'] = True
    _, _, frozen = _contest_scoreboard_access(contest, request.user)
    response['participation'] = _contest_participation_row(participation, frozen=frozen)
    return JsonResponse(response, json_dumps_params={'ensure_ascii': False})


@csrf_protect
@require_http_methods(['POST'])
def cppro_contest_leave(request, identifier):
    profile = _current_profile(request)
    if not profile:
        return _json_error('Authentication required.', 401)

    contest = _find_visible_contest(identifier, request.user)
    if not contest:
        return _json_error('Contest not found.', 404)

    participation = profile.current_contest
    if not participation or participation.contest_id != contest.id:
        return _json_error('You are not currently participating in this contest.', 404)

    # Match DMOJ's native leave behavior: preserve the participation history but
    # clear the active contest pointer so submissions are no longer contest-bound.
    profile.remove_contest()
    contest = _find_visible_contest(identifier, request.user) or contest
    response = _contest_payload(request, contest)
    response['joined'] = False
    response['participation'] = None
    return JsonResponse(response, json_dumps_params={'ensure_ascii': False})


def _find_organization(request, identifier):
    profile = _current_profile(request)
    query = Organization.objects.prefetch_related('admins__user', 'members__user')
    organization = None
    if str(identifier).isdigit():
        organization = query.filter(id=int(identifier)).first()
    if organization is None:
        organization = query.filter(slug=str(identifier)).first()
    if organization is None:
        organization = query.filter(name=str(identifier)).first()
    if not organization or not _can_view_organization(organization, profile, request.user):
        return None, profile
    return organization, profile


def _organization_counts(organizations, request_user):
    organization_ids = [organization.id for organization in organizations]
    problem_counts = {organization_id: 0 for organization_id in organization_ids}
    contest_counts = {organization_id: 0 for organization_id in organization_ids}
    for organization_id in Problem.get_visible_problems(request_user).filter(organizations__id__in=organization_ids).values_list('organizations__id', flat=True):
        problem_counts[organization_id] = problem_counts.get(organization_id, 0) + 1
    for organization_id in _visible_contests_for_user(request_user).filter(organizations__id__in=organization_ids).values_list('organizations__id', flat=True):
        contest_counts[organization_id] = contest_counts.get(organization_id, 0) + 1
    return problem_counts, contest_counts


@require_GET
def cppro_organizations(request):
    profile = _current_profile(request)
    organizations = [
        organization
        for organization in Organization.objects.prefetch_related('admins__user', 'members__user').order_by('name')
        if _can_view_organization(organization, profile, request.user)
    ]
    problem_counts, contest_counts = _organization_counts(organizations, request.user)
    rows = [
        _organization_row(
            organization,
            profile,
            problem_counts,
            contest_counts,
            include_unlisted_admins=_can_manage_organization(organization, profile, request.user),
        )
        for organization in organizations
    ]
    return JsonResponse({'rows': rows, 'total': len(rows)}, json_dumps_params={'ensure_ascii': False})


def _organization_slug(value):
    base = slugify(str(value or '').strip())[:120] or 'organization'
    candidate = base
    suffix = 2
    while Organization.objects.filter(slug=candidate).exists():
        candidate = '%s-%d' % (base[:max(1, 126 - len(str(suffix)))], suffix)
        suffix += 1
    return candidate


@csrf_protect
@require_http_methods(['GET', 'POST', 'PATCH'])
def cppro_admin_organizations(request, identifier=None):
    profile = _current_profile(request)
    if not profile:
        return _json_error('Authentication required.', 401)

    organization = None
    if identifier is not None:
        query = Organization.objects.prefetch_related('admins__user', 'members__user')
        if str(identifier).isdigit():
            organization = query.filter(id=int(identifier)).first()
        if organization is None:
            organization = query.filter(slug=str(identifier)).first()
        if organization is None:
            return _json_error('Organization not found.', 404)
        if not request.user.is_staff and not organization.admins.filter(pk=profile.pk).exists():
            return _json_error('You do not have permission to manage this organization.', 403)
    elif not request.user.is_staff:
        return _json_error('Staff permission is required.', 403)

    if request.method == 'GET':
        organizations = [organization] if organization else list(
            Organization.objects.prefetch_related('admins__user', 'members__user').order_by('name')
        )
        problem_counts, contest_counts = _organization_counts(organizations, request.user)
        rows = [
            _organization_row(
                item,
                profile,
                problem_counts,
                contest_counts,
                include_unlisted_admins=_can_manage_organization(item, profile, request.user),
            )
            for item in organizations
        ]
        return JsonResponse({'rows': rows, 'total': len(rows)}, json_dumps_params={'ensure_ascii': False})

    payload = _parse_json_body(request)
    if request.method == 'POST':
        name = str(payload.get('name') or '').strip()
        if len(name) < 2:
            return _json_error('Organization name is required.', 400)
        requested_slug = str(payload.get('slug') or '').strip()
        organization = Organization.objects.create(
            name=name[:128],
            slug=_organization_slug(requested_slug or name),
            short_name=str(payload.get('shortName') or name)[:20],
            about=str(payload.get('description') or ''),
            is_open=str(payload.get('visibility') or 'private').lower() == 'public',
            is_unlisted=str(payload.get('visibility') or 'private').lower() == 'private',
            logo_override_image=str(payload.get('logoUrl') or '')[:150],
        )
        owner_username = str(payload.get('ownerUsername') or request.user.username).strip()
        owner = Profile.objects.select_related('user').filter(user__username=owner_username).first() or profile
        organization.admins.add(owner)
        owner.organizations.add(organization)
    else:
        if 'name' in payload:
            name = str(payload.get('name') or '').strip()
            if len(name) < 2:
                return _json_error('Organization name is required.', 400)
            organization.name = name[:128]
        if 'shortName' in payload or 'short_name' in payload:
            organization.short_name = str(payload.get('shortName', payload.get('short_name')) or organization.name)[:20]
        if 'description' in payload:
            organization.about = str(payload.get('description') or '')
        if 'logoUrl' in payload or 'logo_url' in payload:
            organization.logo_override_image = str(payload.get('logoUrl', payload.get('logo_url')) or '')[:150]
        if 'visibility' in payload:
            visibility = str(payload.get('visibility') or 'private').lower()
            organization.is_open = visibility == 'public'
            organization.is_unlisted = visibility == 'private'
        organization.save()

    organizations = [
        Organization.objects.prefetch_related('admins__user', 'members__user').get(pk=organization.pk)
    ]
    problem_counts, contest_counts = _organization_counts(organizations, request.user)
    return JsonResponse(
        {
            'organization': _organization_row(
                organizations[0],
                profile,
                problem_counts,
                contest_counts,
                include_unlisted_admins=_can_manage_organization(organizations[0], profile, request.user),
            ),
        },
        status=201 if request.method == 'POST' else 200,
        json_dumps_params={'ensure_ascii': False},
    )


@require_GET
def cppro_organization_detail(request, identifier):
    organization, profile = _find_organization(request, identifier)
    if not organization:
        return _json_error('Organization not found.', 404)
    visible_problems = Problem.get_visible_problems(request.user).filter(organizations=organization).order_by('code')
    visible_contests = _visible_contests_for_user(request.user).filter(organizations=organization).order_by('-start_time', 'key')
    problem_counts = {organization.id: visible_problems.count()}
    contest_counts = {organization.id: visible_contests.count()}
    can_manage = _can_manage_organization(organization, profile, request.user)
    members = []
    admin_ids = set(organization.admins.values_list('id', flat=True))
    member_profile_ids = set()
    member_query = organization.members.select_related('user').order_by('user__username')
    if not can_manage:
        member_query = member_query.filter(is_unlisted=False)
    for member in member_query[:100]:
        member_profile_ids.add(member.id)
        members.append(_organization_member_row(member, 'admin' if member.id in admin_ids else 'member', include_email=can_manage))
    for admin in organization.admins.select_related('user').order_by('user__username'):
        if not can_manage and admin.is_unlisted:
            continue
        if admin.id not in member_profile_ids:
            members.insert(0, _organization_member_row(admin, 'admin', include_email=can_manage))
    pending = None
    if profile:
        request_row = OrganizationRequest.objects.filter(user=profile, organization=organization).order_by('-time').first()
        if request_row:
            pending = {
                'id': request_row.id,
                'status': {'P': 'pending', 'A': 'active', 'R': 'rejected'}.get(request_row.state, request_row.state),
                'created_at': request_row.time.isoformat() if request_row.time else '',
                'reason': request_row.reason or '',
            }
    return JsonResponse({
        'organization': _organization_row(
            organization,
            profile,
            problem_counts,
            contest_counts,
            include_unlisted_admins=can_manage,
        ),
        'canAccess': True,
        'canManage': can_manage,
        'myJoinRequest': pending,
        'members': members,
        'problems': [_organization_problem_row(problem) for problem in visible_problems[:100]],
        'contests': [_organization_contest_row(contest) for contest in visible_contests[:100]],
    }, json_dumps_params={'ensure_ascii': False})


@csrf_protect
@require_http_methods(['POST'])
def cppro_organization_join(request, identifier):
    profile = _current_profile(request)
    if not profile:
        return _json_error('Authentication required.', 401)
    organization, _ = _find_organization(request, identifier)
    if not organization:
        return _json_error('Organization not found.', 404)
    if organization.admins.filter(pk=profile.pk).exists() or profile.organizations.filter(pk=organization.pk).exists():
        return JsonResponse({'joined': True, 'member': _organization_member_row(profile)}, json_dumps_params={'ensure_ascii': False})
    if organization.is_open and not organization.is_unlisted:
        profile.organizations.add(organization)
        organization.member_count = max(int(organization.member_count or 0), organization.members.count())
        organization.save(update_fields=['member_count'])
        return JsonResponse({'joined': True, 'member': _organization_member_row(profile)}, json_dumps_params={'ensure_ascii': False})
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except ValueError:
        payload = {}
    request_row, _ = OrganizationRequest.objects.update_or_create(
        user=profile,
        organization=organization,
        defaults={'state': 'P', 'reason': str(payload.get('note') or payload.get('reason') or '')[:4096]},
    )
    return JsonResponse({
        'joined': False,
        'request': {
            'id': request_row.id,
            'status': 'pending',
            'created_at': request_row.time.isoformat() if request_row.time else '',
        },
    }, json_dumps_params={'ensure_ascii': False})


@require_GET
def cppro_submission_detail(request, submission_id):
    submission = (
        Submission.objects
        .select_related('problem', 'language', 'user__user', 'contest_object')
        .filter(pk=submission_id)
        .first()
    )
    if not submission:
        return _json_error('Submission not found.', 404)
    # Match the legacy submission-detail boundary before returning even
    # metadata. A CPPro manager is also allowed through the bridge-specific
    # server permission check; this is rechecked server-side, never trusted
    # from the client's admin query flag.
    if not (
        (submission.can_see_detail(request.user) or _can_manage_problem(request.user, submission.problem))
        and _can_view_submission_context(submission, request.user)
    ):
        return _json_error('Submission not found.', 404)
    include_tests = str(request.GET.get('includeTests') or '').lower() == 'all'
    admin_context = str(request.GET.get('admin') or '').lower() in {'1', 'true', 'yes'}
    return JsonResponse(
        _submission_detail_row(submission, request.user, include_tests=include_tests, admin_context=admin_context),
        json_dumps_params={'ensure_ascii': False},
    )


@require_GET
def cppro_profile_detail(request, username):
    query = (
        Profile.objects
        .select_related('user')
        .prefetch_related(
            'badges',
            'organizations',
            Prefetch(
                'organizations',
                queryset=Organization.objects.filter(is_unlisted=False),
                to_attr='public_organizations',
            ),
        )
    )
    if str(username).isdigit():
        profile = query.filter(id=int(username)).first() or query.filter(user_id=int(username)).first()
    else:
        profile = query.filter(user__username=username).first()
    if not profile or profile.is_unlisted or not profile.user.is_active:
        return _json_error('User not found.', 404)
    visible_profile_submissions = _visible_submission_queryset(request.user).filter(user=profile)
    stats = _profile_row_for_viewer(
        profile,
        request.user,
        submission_count=visible_profile_submissions.count(),
    )
    recent = [
        _submission_row(item)
        for item in visible_profile_submissions
        .select_related('problem', 'language', 'user__user').order_by('-date')[:20]
    ]
    return JsonResponse({
        'user': stats,
        'stats': stats,
        'recentSubmissions': recent,
        'ratingHistory': [],
        'activityHeatmap': [],
        'solvedTags': [],
        'solvedProblems': [],
        'unfinishedProblems': [],
        'badges': [{'name': name} for name in stats.get('badges', [])],
    }, json_dumps_params={'ensure_ascii': False})


@csrf_protect
@require_http_methods(['GET', 'POST'])
def cppro_create_submission(request):
    if request.method == 'GET':
        return JsonResponse(_submission_list_payload(request), json_dumps_params={'ensure_ascii': False})

    profile = _current_profile(request)
    if not profile:
        return _json_error('Authentication required.', 401)

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except ValueError:
        return _json_error('Invalid JSON body.', 400)

    problem_identifier = payload.get('problemId') or payload.get('problem_id') or payload.get('problemSlug') or payload.get('problem_slug')
    language_key = str(payload.get('language') or '').strip()
    source = str(payload.get('code') or '').replace('\r\n', '\n')

    if not problem_identifier:
        return _json_error('Problem is required.', 400)
    if not language_key:
        return _json_error('Language is required.', 400)
    if not source.strip():
        return _json_error('Source code is required.', 400)
    if len(source) > 65536:
        return _json_error('Source code is too large. The limit is 64 KiB.', 413)

    visible = Problem.get_visible_problems(request.user)
    problem = None
    if str(problem_identifier).isdigit():
        problem = visible.filter(id=int(problem_identifier)).first()
    if problem is None:
        problem = visible.filter(code=str(problem_identifier)).first()
    if problem is None:
        return _json_error('Problem not found.', 404)
    _ensure_problem_has_judge(problem)

    language = Language.objects.filter(key=language_key).first()
    if language is None:
        return _json_error('Language not found.', 404)

    allowed_languages = problem.allowed_languages.all()
    if allowed_languages.exists() and not allowed_languages.filter(pk=language.pk).exists():
        return _json_error('This language is not enabled for the problem.', 400)

    contest = None
    contest_identifier = payload.get('contestId') or payload.get('contest_id') or payload.get('contestSlug') or payload.get('contest_slug')
    if contest_identifier:
        contest_query = _visible_contests_for_user(request.user)
        if str(contest_identifier).isdigit():
            contest = contest_query.filter(id=int(contest_identifier)).first()
        if contest is None:
            contest = contest_query.filter(key=str(contest_identifier)).first()
        if contest is None:
            return _json_error('Contest not found.', 404)
        contest_problem = ContestProblem.objects.filter(contest=contest, problem=problem).select_related('problem').first()
        if contest_problem is None:
            return _json_error('This problem is not part of the selected contest.', 400)
        participation = (
            ContestParticipation.objects
            .filter(contest=contest, user=profile)
            .order_by('-virtual', '-id')
            .first()
        )
        if not participation or participation.pre_registered or participation.spectate or participation.is_disqualified:
            return _json_error('Join the contest before submitting this contest problem.', 403)
        if participation.ended or not contest.can_join:
            return _json_error('This contest is not accepting submissions.', 403)
        if profile.current_contest_id != participation.id:
            profile.current_contest = participation
            profile.save(update_fields=['current_contest'])

    with transaction.atomic():
        submission = Submission.objects.create(
            user=profile,
            problem=problem,
            language=language,
            contest_object=contest,
        )
        source_row = SubmissionSource.objects.create(submission=submission, source=source)

    submission.source = source_row
    judge_warning = ''
    try:
        submission.judge(force_judge=True)
    except Exception as exc:
        internal_error = str(exc)[:400]
        judge_warning = 'The submission could not be queued for judging. Please try again shortly.'
        submission.status = 'IE'
        submission.error = internal_error
        submission.save(update_fields=['status', 'error'])

    submission = Submission.objects.select_related('problem', 'language', 'user__user', 'contest_object').get(pk=submission.pk)
    if contest is not None and contest_problem is not None and participation is not None:
        ContestSubmission.objects.update_or_create(
            submission=submission,
            defaults={
                'problem': contest_problem,
                'participation': participation,
                'points': _contest_submission_score(submission, contest_problem),
                'is_pretest': bool(submission.is_pretested),
            },
        )
        participation.recompute_results()
    response = {
        'id': submission.id,
        'submission_id': submission.id,
        'submission': _submission_row(submission),
    }
    if judge_warning:
        response['warning'] = judge_warning
    return JsonResponse(response, status=201 if not judge_warning else 202, json_dumps_params={'ensure_ascii': False})


@csrf_protect
@require_http_methods(['GET', 'PATCH', 'POST'])
def cppro_profile(request):
    profile = _current_profile(request)
    if not profile:
        return _json_error('Authentication required.', 401)
    if request.method == 'GET':
        return JsonResponse(_auth_user_row(profile), json_dumps_params={'ensure_ascii': False})

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except ValueError:
        return _json_error('Invalid JSON body.', 400)

    user = profile.user
    changed_user = []
    changed_profile = []

    if 'fullName' in payload:
        full_name = str(payload.get('fullName') or '').strip()[:100]
        profile.username_display_override = '' if not full_name or full_name == user.username else full_name
        user.first_name = full_name
        user.last_name = ''
        changed_profile.append('username_display_override')
        changed_user.extend(['first_name', 'last_name'])

    if 'email' in payload:
        user.email = str(payload.get('email') or '').strip()[:254]
        changed_user.append('email')

    if 'avatarUrl' in payload:
        meta = _read_cppro_meta(profile)
        avatar_url = str(payload.get('avatarUrl') or '').strip()
        if avatar_url:
            meta['avatar_url'] = avatar_url[:4096]
        else:
            meta.pop('avatar_url', None)
        _write_cppro_meta(profile, meta)
        changed_profile.append('notes')

    if 'favoriteLanguage' in payload or 'favorite_language' in payload:
        favorite_language = str(payload.get('favoriteLanguage', payload.get('favorite_language')) or '').strip()
        if favorite_language and not Language.objects.filter(key=favorite_language).exists():
            return _json_error('Favorite language is not available on this judge.', 400)
        meta = _read_cppro_meta(profile)
        if favorite_language:
            meta['favorite_language'] = favorite_language[:50]
        else:
            meta.pop('favorite_language', None)
        _write_cppro_meta(profile, meta)
        changed_profile.append('notes')

    new_password = str(payload.get('newPassword') or '')
    if new_password:
        current_password = str(payload.get('currentPassword') or '')
        if not current_password or not user.check_password(current_password):
            return _json_error('Current password is incorrect.', 400)
        if len(new_password) < 8:
            return _json_error('New password must be at least 8 characters.', 400)
        user.set_password(new_password)
        changed_user.append('password')

    if changed_user:
        user.save(update_fields=sorted(set(changed_user)))
    if changed_profile:
        profile.save(update_fields=sorted(set(changed_profile)))
    if new_password:
        update_session_auth_hash(request, user)

    profile = Profile.objects.select_related('user').prefetch_related('badges', 'organizations').get(pk=profile.pk)
    return JsonResponse(_auth_user_row(profile), json_dumps_params={'ensure_ascii': False})


def _admin_problem_row(problem):
    return {
        'id': problem.id,
        'slug': problem.code,
        'code': problem.code,
        'title': problem.name,
        'description': problem.description or '',
        'source': problem.source or '',
        'time_limit': float(problem.time_limit or 0),
        'time_limit_ms': int(float(problem.time_limit or 0) * 1000),
        'memory_limit': int(problem.memory_limit or 0),
        'memory_limit_mb': max(1, int(round(float(problem.memory_limit or 0) / 1024))),
        'points': float(problem.points or 0),
        'partial': bool(problem.partial),
        'is_public': bool(problem.is_public),
        'summary': problem.summary or '',
        'group': {
            'id': problem.group_id,
            'name': getattr(problem.group, 'name', ''),
            'full_name': getattr(problem.group, 'full_name', ''),
        },
        'types': [
            {'id': item.id, 'name': item.name, 'full_name': item.full_name}
            for item in problem.types.order_by('full_name', 'name')
        ],
        'allowed_languages': list(problem.allowed_languages.order_by('key').values_list('key', flat=True)),
        'submission_source_visibility_mode': problem.submission_source_visibility_mode,
        'testcase_visibility_mode': problem.testcase_visibility_mode,
        'testcase_result_visibility_mode': problem.testcase_result_visibility_mode,
    }


def _admin_problem_from_identifier(request, identifier):
    query = Problem.objects.select_related('group').prefetch_related('types', 'allowed_languages', 'authors__user', 'curators__user')
    problem = query.filter(id=int(identifier)).first() if str(identifier).isdigit() else None
    if problem is None:
        problem = query.filter(code=str(identifier)).first()
    if problem is None:
        return None, _json_error('Problem not found.', 404)
    if not _can_manage_problem(request.user, problem):
        return None, _json_error('You do not have permission to manage this problem.', 403)
    return problem, None


def _admin_payload(request):
    if request.FILES:
        return {key: value for key, value in request.POST.items()}
    payload = _parse_json_body(request)
    return payload if isinstance(payload, dict) else {}


def _admin_boolean(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _choice_values(choices):
    if isinstance(choices, dict):
        return set(str(key) for key in choices)
    values = set()
    for choice in choices or ():
        if isinstance(choice, (tuple, list)):
            values.add(str(choice[0]))
        else:
            values.add(str(choice))
    return values


def _problem_data_archive_files(data):
    if not data or not data.zipfile:
        return []
    try:
        with data.zipfile.storage.open(data.zipfile.name, 'rb') as handle:
            with ZipFile(handle) as archive:
                return sorted(
                    name.replace('\\', '/')
                    for name in archive.namelist()
                    if name and not name.endswith('/')
                )
    except (BadZipFile, OSError, ValueError):
        return []


def _problem_data_grader_args(data):
    raw = getattr(data, 'grader_args', '') or ''
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _natural_file_key(value):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r'(\d+)', value)]


def _auto_testcase_rows(problem, valid_files):
    # Pair conventional input/output file names in the archive so a newly
    # uploaded ZIP can immediately be compiled into the judge's init.yml.
    file_map = {str(name).casefold(): str(name) for name in valid_files}
    pairs = []
    output_extensions = {
        '.in': ('.out', '.ans', '.output'),
        '.inp': ('.out', '.ans', '.output'),
        '.input': ('.out', '.ans', '.output'),
    }
    for input_name in sorted(valid_files, key=_natural_file_key):
        stem, extension = os.path.splitext(str(input_name))
        for output_extension in output_extensions.get(extension.casefold(), ()):
            output_name = file_map.get((stem + output_extension).casefold())
            if output_name:
                pairs.append((str(input_name), output_name))
                break
    if not pairs:
        return []
    total_points = int(round(float(problem.points or 0)))
    if total_points <= 0:
        total_points = 100
    if total_points >= len(pairs):
        base_points, remainder = divmod(total_points, len(pairs))
        return [
            {
                'order': index,
                'type': 'C',
                'input_file': input_name,
                'output_file': output_name,
                'points': base_points + (1 if index <= remainder else 0),
                'is_pretest': False,
            }
            for index, (input_name, output_name) in enumerate(pairs, start=1)
        ]

    # ProblemTestCase stores integer points. When there are more files than
    # points, use batches so every test contributes to one of the point groups
    # rather than silently assigning zero-point tests.
    rows = []
    position = 1
    cursor = 0
    base_size, remainder = divmod(len(pairs), total_points)
    for group_index in range(total_points):
        group_size = base_size + (1 if group_index < remainder else 0)
        rows.append({
            'order': position,
            'type': 'S',
            'input_file': '',
            'output_file': '',
            'points': 1,
            'is_pretest': False,
        })
        position += 1
        for input_name, output_name in pairs[cursor:cursor + group_size]:
            rows.append({
                'order': position,
                'type': 'C',
                'input_file': input_name,
                'output_file': output_name,
                'points': 0,
                'is_pretest': False,
            })
            position += 1
        cursor += group_size
        rows.append({
            'order': position,
            'type': 'E',
            'input_file': '',
            'output_file': '',
            'points': 0,
            'is_pretest': False,
        })
        position += 1
    return rows


def _parse_admin_testcases(payload):
    if 'testcases' not in payload:
        return None
    rows = payload.get('testcases')
    if isinstance(rows, str):
        try:
            rows = json.loads(rows or '[]')
        except ValueError:
            raise ValueError('testcases must be valid JSON.')
    if not isinstance(rows, list):
        raise ValueError('testcases must be a list.')
    return rows


def _replace_admin_testcases(problem, data, rows, valid_files):
    existing = {case.id: case for case in problem.cases.all()}
    selected_ids = set()
    valid_file_map = {str(name).casefold(): str(name) for name in valid_files}
    configured_orders = set()
    normalized_rows = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError('Each testcase must be an object.')
        raw_id = row.get('id')
        case = existing.get(int(raw_id)) if str(raw_id or '').isdigit() else None
        if raw_id not in (None, '', 0) and not case:
            raise ValueError('A testcase does not belong to this problem.')
        try:
            order = int(row.get('order') or index)
        except (TypeError, ValueError):
            raise ValueError('Testcase order must be a whole number.')
        if order <= 0 or order in configured_orders:
            raise ValueError('Each testcase must have a unique positive order.')
        configured_orders.add(order)
        case_type = str(row.get('type') or 'C').strip().upper()
        if case_type not in {'C', 'S', 'E'}:
            raise ValueError('Testcase type is invalid.')
        input_file = str(row.get('input_file') or row.get('inputFile') or '').strip().replace('\\', '/')
        output_file = str(row.get('output_file') or row.get('outputFile') or '').strip().replace('\\', '/')
        generator_args = str(row.get('generator_args') or row.get('generatorArgs') or '').strip()
        if case_type == 'C' and (not input_file or not output_file):
            raise ValueError('Standard testcases need both an input file and an output file.')
        if valid_file_map:
            if input_file and input_file.casefold() not in valid_file_map:
                raise ValueError('Input file %s is not in the ZIP archive.' % input_file)
            if output_file and output_file.casefold() not in valid_file_map:
                raise ValueError('Output file %s is not in the ZIP archive.' % output_file)
            input_file = valid_file_map.get(input_file.casefold(), input_file)
            output_file = valid_file_map.get(output_file.casefold(), output_file)
        try:
            points = float(row.get('points', 0) or 0)
        except (TypeError, ValueError):
            raise ValueError('Testcase points must be numeric.')
        if points < 0:
            raise ValueError('Testcase points cannot be negative.')
        normalized_rows.append({
            'case': case,
            'order': order,
            'type': case_type,
            'input_file': input_file,
            'output_file': output_file,
            'generator_args': generator_args,
            'points': points,
            'is_pretest': _admin_boolean(row.get('is_pretest', row.get('isPretest'))),
            'checker': str(row.get('checker') or '').strip()[:10],
        })

    for values in normalized_rows:
        case = values.pop('case')
        if case:
            selected_ids.add(case.id)
            for key, value in values.items():
                setattr(case, key, value)
            case.save(update_fields=list(values))
        else:
            ProblemTestCase.objects.create(dataset=problem, **values)
    stale_ids = set(existing) - selected_ids
    if stale_ids:
        ProblemTestCase.objects.filter(id__in=stale_ids).delete()


def _compile_problem_data(problem, data, valid_files):
    try:
        ProblemDataCompiler.generate(problem, data, problem.cases.order_by('order', 'id'), valid_files)
        data.refresh_from_db()
    except Exception as exc:
        data.feedback = str(exc)[:4000]
        data.save(update_fields=['feedback'])
    return str(data.feedback or '')


def _admin_testcase_row(case):
    return {
        'id': case.id,
        'order': int(case.order or 0),
        'type': case.type,
        'input_file': case.input_file or '',
        'output_file': case.output_file or '',
        'points': float(case.points or 0),
        'is_pretest': bool(case.is_pretest),
        'checker': case.checker or '',
        'generator_args': case.generator_args or '',
    }


def _admin_ticket_row(ticket):
    return {
        'id': ticket.id,
        'title': ticket.title,
        'is_open': bool(ticket.is_open),
        'notes': ticket.notes or '',
        'created_at': ticket.time.isoformat() if ticket.time else '',
        'username': ticket.user.user.username,
        'full_name': _author_name(ticket.user),
        'assignees': list(ticket.assignees.values_list('user__username', flat=True)),
    }


def _admin_problem_data_row(problem):
    try:
        data = problem.data_files
    except ProblemData.DoesNotExist:
        data = None
    grader_args = _problem_data_grader_args(data) if data else {}
    has_yml = False
    if data:
        try:
            has_yml = bool(data.has_yml())
        except (AttributeError, OSError):
            has_yml = False
    return {
        'has_data': bool(data),
        'zipfile_name': data.zipfile.name if data and data.zipfile else '',
        'generator_name': data.generator.name if data and data.generator else '',
        'checker': data.checker if data else 'standard',
        'grader': data.grader if data else 'standard',
        'io_method': 'file' if grader_args.get('io_method') == 'file' else 'standard',
        'io_input_file': str(grader_args.get('io_input_file') or ''),
        'io_output_file': str(grader_args.get('io_output_file') or ''),
        'output_limit': data.output_limit if data and data.output_limit is not None else '',
        'valid_files': _problem_data_archive_files(data),
        'has_init_yml': has_yml,
        'feedback': data.feedback if data else '',
    }


def _admin_problem_action_payload(request, problem, action):
    base = {
        'action': action,
        'problem': _admin_problem_row(problem),
        'groups': [
            {'id': group.id, 'name': group.name, 'full_name': group.full_name}
            for group in ProblemGroup.objects.order_by('full_name', 'name')
        ],
        'types': [
            {'id': item.id, 'name': item.name, 'full_name': item.full_name}
            for item in ProblemType.objects.order_by('full_name', 'name')
        ],
        'languages': [
            {'key': language.key, 'name': language.name}
            for language in Language.objects.order_by('key')
        ],
    }
    if action == 'testcases':
        base['data'] = _admin_problem_data_row(problem)
        base['testcases'] = [_admin_testcase_row(case) for case in problem.cases.order_by('order', 'id')]
    elif action == 'tickets':
        content_type = ContentType.objects.get_for_model(Problem)
        base['tickets'] = [
            _admin_ticket_row(ticket)
            for ticket in Ticket.objects.filter(content_type=content_type, object_id=problem.id).select_related('user__user').prefetch_related('assignees__user').order_by('-is_open', '-time')
        ]
    elif action == 'manage-submissions':
        submissions = (
            Submission.objects
            .filter(problem=problem)
            .select_related('problem', 'language', 'user__user', 'contest_object')
            .order_by('-date', '-id')[:150]
        )
        base['submissions'] = [_submission_row(submission) for submission in submissions]
        selected_id = request.GET.get('submissionId')
        if selected_id and str(selected_id).isdigit():
            selected = next((item for item in submissions if item.id == int(selected_id)), None)
            if selected:
                base['selectedSubmission'] = _submission_detail_row(selected, request.user, include_tests=True, admin_context=True)
    elif action == 'clone':
        base['clone_defaults'] = {
            'code': '%s_copy' % problem.code,
            'title': '%s (copy)' % problem.name,
            'copy_data': True,
        }
    return base


def _copy_problem_data(source_problem, target_problem):
    try:
        source_data = source_problem.data_files
    except ProblemData.DoesNotExist:
        source_data = None
    if not source_data:
        return
    target_data = ProblemData(
        problem=target_problem,
        output_prefix=source_data.output_prefix,
        output_limit=source_data.output_limit,
        feedback=source_data.feedback,
        checker=source_data.checker,
        grader=source_data.grader,
        unicode=source_data.unicode,
        nobigmath=source_data.nobigmath,
        checker_args=source_data.checker_args,
        grader_args=source_data.grader_args,
    )
    for field_name in ['zipfile', 'generator', 'custom_checker', 'custom_grader', 'custom_header']:
        source_file = getattr(source_data, field_name)
        if not source_file:
            continue
        try:
            with source_file.storage.open(source_file.name, 'rb') as handle:
                getattr(target_data, field_name).save(os.path.basename(source_file.name), ContentFile(handle.read()), save=False)
        except OSError:
            continue
    target_data.save()
    for case in source_problem.cases.order_by('order', 'id'):
        ProblemTestCase.objects.create(
            dataset=target_problem,
            order=case.order,
            type=case.type,
            input_file=case.input_file,
            output_file=case.output_file,
            generator_args=case.generator_args,
            points=case.points,
            is_pretest=case.is_pretest,
            output_prefix=case.output_prefix,
            output_limit=case.output_limit,
            checker=case.checker,
            checker_args=case.checker_args,
        )


@csrf_protect
@require_http_methods(['GET', 'PATCH', 'POST'])
def cppro_problem_admin(request, identifier, action):
    action = str(action or '').strip().lower()
    if action not in {'edit', 'type-group', 'testcases', 'tickets', 'manage-submissions', 'clone'}:
        return _json_error('Unknown problem management action.', 404)
    problem, error = _admin_problem_from_identifier(request, identifier)
    if error:
        return error
    if request.method == 'GET':
        return JsonResponse(_admin_problem_action_payload(request, problem, action), json_dumps_params={'ensure_ascii': False})

    payload = _admin_payload(request)
    if action == 'edit':
        title = str(payload.get('title') or problem.name).strip()
        if not title:
            return _json_error('Problem title is required.', 400)
        try:
            time_limit = float(payload.get('timeLimit') if 'timeLimit' in payload else payload.get('time_limit', problem.time_limit))
            memory_mb = float(payload.get('memoryLimitMb') if 'memoryLimitMb' in payload else payload.get('memory_limit_mb', problem.memory_limit / 1024))
            points = float(payload.get('points', problem.points))
        except (TypeError, ValueError):
            return _json_error('Time limit, memory limit, and points must be numeric.', 400)
        if time_limit <= 0 or memory_mb <= 0 or points < 0:
            return _json_error('Time limit and memory limit must be positive; points cannot be negative.', 400)
        problem.name = title[:100]
        if 'description' in payload:
            problem.description = str(payload.get('description') or '')
        if 'summary' in payload:
            problem.summary = str(payload.get('summary') or '')[:10000]
        if 'source' in payload:
            problem.source = str(payload.get('source') or '')[:200]
        problem.time_limit = time_limit
        problem.memory_limit = max(1, int(round(memory_mb * 1024)))
        problem.points = points
        if 'partial' in payload:
            problem.partial = _admin_boolean(payload.get('partial'))
        if 'isPublic' in payload or 'is_public' in payload:
            problem.is_public = _admin_boolean(payload.get('isPublic', payload.get('is_public')))
        if 'allowedLanguages' in payload or 'allowed_languages' in payload:
            raw_languages = payload.get('allowedLanguages', payload.get('allowed_languages'))
            if isinstance(raw_languages, str):
                raw_languages = [item.strip() for item in raw_languages.split(',') if item.strip()]
            if not isinstance(raw_languages, list):
                return _json_error('allowedLanguages must be a list.', 400)
            languages = list(Language.objects.filter(key__in=[str(item) for item in raw_languages]))
            if len(languages) != len(set(str(item) for item in raw_languages)):
                return _json_error('One or more selected languages do not exist.', 400)
            problem.save()
            problem.allowed_languages.set(languages)
        else:
            problem.save()
    elif action == 'type-group':
        group_identifier = payload.get('groupId', payload.get('group_id'))
        if group_identifier not in (None, ''):
            group = ProblemGroup.objects.filter(id=int(group_identifier)).first() if str(group_identifier).isdigit() else ProblemGroup.objects.filter(name=str(group_identifier)).first()
            if not group:
                return _json_error('Problem group not found.', 404)
            problem.group = group
            problem.save(update_fields=['group'])
        raw_types = payload.get('typeIds', payload.get('type_ids', []))
        if isinstance(raw_types, str):
            raw_types = [item.strip() for item in raw_types.split(',') if item.strip()]
        if not isinstance(raw_types, list):
            return _json_error('typeIds must be a list.', 400)
        type_ids = [int(item) for item in raw_types if str(item).isdigit()]
        types = list(ProblemType.objects.filter(id__in=type_ids))
        if len(types) != len(set(type_ids)):
            return _json_error('One or more selected types do not exist.', 400)
        problem.types.set(types)
    elif action == 'testcases':
        try:
            testcase_rows = _parse_admin_testcases(payload)
        except ValueError as exc:
            return _json_error(str(exc), 400)
        try:
            with transaction.atomic():
                data, _ = ProblemData.objects.get_or_create(problem=problem)
                zipfile = request.FILES.get('zipfile')
                generator = request.FILES.get('generator')
                if zipfile:
                    data.zipfile = zipfile
                if generator:
                    data.generator = generator
                if 'checker' in payload:
                    checker = str(payload.get('checker') or 'standard').strip()
                    if checker not in _choice_values(CHECKERS):
                        return _json_error('Checker is not supported by this judge.', 400)
                    data.checker = checker
                if 'grader' in payload:
                    grader = str(payload.get('grader') or 'standard').strip()
                    if grader not in _choice_values(GRADERS):
                        return _json_error('Grader is not supported by this judge.', 400)
                    data.grader = grader
                io_method = str(payload.get('ioMethod', payload.get('io_method', _problem_data_grader_args(data).get('io_method', 'standard'))) or 'standard').strip().lower()
                if io_method not in {'standard', 'file'}:
                    return _json_error('IO method must be standard or file.', 400)
                io_input_file = str(payload.get('ioInputFile', payload.get('io_input_file', _problem_data_grader_args(data).get('io_input_file', ''))) or '').strip()
                io_output_file = str(payload.get('ioOutputFile', payload.get('io_output_file', _problem_data_grader_args(data).get('io_output_file', ''))) or '').strip()
                if io_method == 'file':
                    if not io_input_file or not io_output_file:
                        return _json_error('File IO needs both the input and output file names.', 400)
                    data.grader_args = json.dumps({
                        'io_method': 'file',
                        'io_input_file': io_input_file,
                        'io_output_file': io_output_file,
                    })
                else:
                    data.grader_args = ''
                if 'outputLimit' in payload or 'output_limit' in payload:
                    raw_output_limit = payload.get('outputLimit', payload.get('output_limit'))
                    if raw_output_limit in (None, ''):
                        data.output_limit = None
                    else:
                        try:
                            data.output_limit = max(1, int(raw_output_limit))
                        except (TypeError, ValueError):
                            return _json_error('Output limit must be a positive whole number.', 400)
                data.save()
                valid_files = _problem_data_archive_files(data)
                if testcase_rows is not None:
                    _replace_admin_testcases(problem, data, testcase_rows, valid_files)
                elif _admin_boolean(payload.get('autoDetect'), bool(zipfile and not problem.cases.exists())):
                    detected_rows = _auto_testcase_rows(problem, valid_files)
                    if not detected_rows:
                        return _json_error('No matching input/output testcase pairs were found in the ZIP archive.', 400)
                    _replace_admin_testcases(problem, data, detected_rows, valid_files)
                feedback = _compile_problem_data(problem, data, valid_files)
                if feedback:
                    return _json_error(feedback, 400)
        except ValueError as exc:
            return _json_error(str(exc), 400)
    elif action == 'tickets':
        ticket_id = payload.get('ticketId', payload.get('ticket_id'))
        if not ticket_id or not str(ticket_id).isdigit():
            return _json_error('ticketId is required.', 400)
        content_type = ContentType.objects.get_for_model(Problem)
        ticket = Ticket.objects.filter(id=int(ticket_id), content_type=content_type, object_id=problem.id).first()
        if not ticket:
            return _json_error('Ticket not found.', 404)
        if 'isOpen' in payload or 'is_open' in payload:
            ticket.is_open = _admin_boolean(payload.get('isOpen', payload.get('is_open')))
        if 'notes' in payload:
            ticket.notes = str(payload.get('notes') or '')[:20000]
        ticket.save(update_fields=['is_open', 'notes'])
    elif action == 'manage-submissions':
        submission_id = payload.get('submissionId', payload.get('submission_id'))
        if not submission_id or not str(submission_id).isdigit():
            return _json_error('submissionId is required.', 400)
        submission = Submission.objects.filter(id=int(submission_id), problem=problem).first()
        if not submission:
            return _json_error('Submission not found.', 404)
        try:
            submission.judge(rejudge=True, rejudge_user=request.user)
        except Exception as exc:
            return _json_error('Could not queue rejudge: %s' % str(exc)[:300], 409)
    elif action == 'clone':
        code = str(payload.get('code') or '').strip().lower()
        title = str(payload.get('title') or '').strip()
        if not re.match(r'^[a-z0-9_]{1,32}$', code):
            return _json_error('Clone code must contain only lowercase letters, numbers, and underscores.', 400)
        if not title:
            return _json_error('Clone title is required.', 400)
        if Problem.objects.filter(code=code).exists():
            return _json_error('A problem with this code already exists.', 409)
        with transaction.atomic():
            clone = Problem.objects.create(
                code=code,
                name=title[:100],
                pdf_url=problem.pdf_url,
                source=problem.source,
                description=problem.description,
                group=problem.group,
                time_limit=problem.time_limit,
                memory_limit=problem.memory_limit,
                short_circuit=problem.short_circuit,
                points=problem.points,
                partial=problem.partial,
                is_public=False,
                is_manually_managed=problem.is_manually_managed,
                license=problem.license,
                og_image=problem.og_image,
                summary=problem.summary,
                is_full_markup=problem.is_full_markup,
                submission_source_visibility_mode=problem.submission_source_visibility_mode,
                testcase_visibility_mode=problem.testcase_visibility_mode,
                testcase_result_visibility_mode=problem.testcase_result_visibility_mode,
                is_organization_private=problem.is_organization_private,
                suggester=problem.suggester,
                allow_view_feedback=problem.allow_view_feedback,
            )
            clone.allowed_languages.set(problem.allowed_languages.all())
            clone.types.set(problem.types.all())
            clone.authors.set(problem.authors.all())
            clone.curators.set(problem.curators.all())
            clone.testers.set(problem.testers.all())
            clone.banned_users.set(problem.banned_users.all())
            clone.organizations.set(problem.organizations.all())
            clone.judges.add(*problem.judges.all())
            if _admin_boolean(payload.get('copyData', payload.get('copy_data')), True):
                _copy_problem_data(problem, clone)
        return JsonResponse({
            'created': True,
            'problem': _admin_problem_row(clone),
        }, status=201, json_dumps_params={'ensure_ascii': False})

    problem = Problem.objects.select_related('group').prefetch_related('types', 'allowed_languages', 'authors__user', 'curators__user').get(pk=problem.pk)
    return JsonResponse(_admin_problem_action_payload(request, problem, action), json_dumps_params={'ensure_ascii': False})


def _management_problem_row(problem):
    row = _admin_problem_row(problem)
    row.update({
        'external_id': problem.code,
        'difficulty': _difficulty(problem),
        'rating': max(0.1, min(5.0, float(problem.points or 0) / 100.0 or 1.0)),
        'score': float(problem.points or 0),
        'timeLimitMs': int(float(problem.time_limit or 1) * 1000),
        'memoryLimitMb': max(1, int(round(float(problem.memory_limit or 0) / 1024))),
        'visibility': 'public' if problem.is_public else 'private',
        'judge_run_all': bool(problem.partial),
        'tags': [
            {'id': item.id, 'name': item.full_name or item.name, 'slug': item.name}
            for item in problem.types.order_by('full_name', 'name')
        ],
    })
    return row


def _normalized_identifier(value, fallback, maximum=32):
    candidate = re.sub(r'[^a-z0-9]+', '', str(value or '').strip().lower())
    if not candidate:
        candidate = re.sub(r'[^a-z0-9]+', '', slugify(str(fallback or 'item')).lower())
    return (candidate or 'item')[:maximum]


def _unique_model_code(model, requested, fallback, field='code'):
    base = _normalized_identifier(requested, fallback)
    candidate = base
    suffix = 2
    while model.objects.filter(**{field: candidate}).exists():
        tail = str(suffix)
        candidate = '%s%s' % (base[:max(1, 32 - len(tail))], tail)
        suffix += 1
    return candidate


def _problem_time_seconds(value, default=1.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed >= 100:
        parsed /= 1000.0
    return max(0.05, min(parsed, 120.0))


def _problem_memory_kilobytes(value, default=262144):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 4096:
        parsed *= 1024
    return max(1024, min(int(round(parsed)), 16 * 1024 * 1024))


def _apply_problem_languages_and_tags(problem, payload):
    if 'allowedLanguages' in payload or 'allowed_languages' in payload:
        requested = payload.get('allowedLanguages', payload.get('allowed_languages'))
        if requested is None:
            requested = []
        if isinstance(requested, str):
            requested = [item.strip() for item in requested.split(',') if item.strip()]
        if not isinstance(requested, list):
            raise ValueError('allowedLanguages must be a list or null.')
        keys = [str(item) for item in requested]
        languages = list(Language.objects.filter(key__in=keys))
        if len(languages) != len(set(keys)):
            raise ValueError('One or more selected languages do not exist.')
        problem.allowed_languages.set(languages)

    if 'tags' in payload:
        requested = payload.get('tags') or []
        if isinstance(requested, str):
            requested = [item.strip() for item in requested.split(',') if item.strip()]
        if not isinstance(requested, list):
            raise ValueError('tags must be a list.')
        types = []
        for raw in requested[:30]:
            label = str(raw.get('name') if isinstance(raw, dict) else raw).strip()
            if not label:
                continue
            key = _normalized_identifier(label, label, maximum=20)
            item, _ = ProblemType.objects.get_or_create(
                name=key,
                defaults={'full_name': label[:100]},
            )
            types.append(item)
        problem.types.set(types)


def _store_inline_problem_testcases(problem, raw_rows):
    if not isinstance(raw_rows, list) or not raw_rows:
        return None
    if len(raw_rows) > 100:
        raise ValueError('A maximum of 100 testcases can be created at once.')
    archive_buffer = BytesIO()
    parsed_rows = []
    valid_files = []
    with ZipFile(archive_buffer, 'w') as archive:
        for index, raw in enumerate(raw_rows, start=1):
            row = raw if isinstance(raw, dict) else {}
            input_name = 'case%03d.in' % index
            output_name = 'case%03d.out' % index
            archive.writestr(input_name, str(row.get('input') or row.get('inputData') or ''))
            archive.writestr(output_name, str(row.get('output') or row.get('expectedOutput') or ''))
            valid_files.extend([input_name, output_name])
            parsed_rows.append({
                'order': index,
                'type': 'C',
                'input_file': input_name,
                'output_file': output_name,
                'points': max(0, int(row.get('points') or 1)),
                'is_pretest': bool(row.get('isSample') or row.get('is_sample')),
            })
    data, _ = ProblemData.objects.get_or_create(problem=problem)
    data.zipfile.save('%s-tests.zip' % problem.code, ContentFile(archive_buffer.getvalue()), save=True)
    _replace_admin_testcases(problem, data, parsed_rows, valid_files)
    feedback = _compile_problem_data(problem, data, valid_files)
    if feedback:
        raise ValueError(feedback)
    return data


@csrf_protect
@require_http_methods(['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
def cppro_problems(request, identifier=None):
    if request.method == 'GET':
        if request.user.is_authenticated and request.user.is_staff:
            query = Problem.objects.all()
        else:
            query = Problem.get_visible_problems(request.user)
        query = query.select_related('group').prefetch_related('types', 'allowed_languages', 'authors__user')
        if identifier is None:
            rows = [_management_problem_row(problem) for problem in query.order_by('code')[:1000]]
            return JsonResponse({'rows': rows, 'total': query.count()}, json_dumps_params={'ensure_ascii': False})
        problem = query.filter(id=int(identifier)).first() if str(identifier).isdigit() else None
        if problem is None:
            problem = query.filter(code=str(identifier)).first()
        if problem is None:
            return _json_error('Problem not found.', 404)
        return JsonResponse(_management_problem_row(problem), json_dumps_params={'ensure_ascii': False})

    denied = _require_platform_admin(request)
    if denied:
        return denied
    payload = _admin_payload(request)
    if request.method == 'POST':
        title = str(payload.get('title') or '').strip()
        if len(title) < 3:
            return _json_error('Problem title must contain at least three characters.', 400)
        group = ProblemGroup.objects.order_by('id').first()
        if group is None:
            group = ProblemGroup.objects.create(name='uncategorized', full_name='Uncategorized')
        code = _unique_model_code(Problem, payload.get('externalId'), title)
        visibility = str(payload.get('visibility') or 'private').lower()
        try:
            with transaction.atomic():
                problem = Problem.objects.create(
                    code=code,
                    name=title[:100],
                    description=str(payload.get('description') or ''),
                    group=group,
                    time_limit=_problem_time_seconds(payload.get('timeLimit'), 1.0),
                    memory_limit=_problem_memory_kilobytes(payload.get('memoryLimit'), 262144),
                    points=max(0.0, float(payload.get('points') or (float(payload.get('rating') or 1) * 100))),
                    partial=str(payload.get('scoringMode') or '').lower() == 'partial',
                    is_public=visibility == 'public',
                )
                if hasattr(request.user, 'profile'):
                    problem.authors.add(request.user.profile)
                _apply_problem_languages_and_tags(problem, payload)
                _ensure_problem_has_judge(problem)
                _store_inline_problem_testcases(problem, payload.get('testCases'))
        except (TypeError, ValueError) as error:
            return _json_error(str(error), 400)
        return JsonResponse(_management_problem_row(problem), status=201, json_dumps_params={'ensure_ascii': False})

    problem = Problem.objects.select_related('group').prefetch_related('types', 'allowed_languages').filter(
        id=int(identifier),
    ).first() if str(identifier).isdigit() else Problem.objects.select_related('group').prefetch_related('types', 'allowed_languages').filter(code=str(identifier)).first()
    if problem is None:
        return _json_error('Problem not found.', 404)
    if request.method == 'DELETE':
        deleted = {'id': problem.id, 'code': problem.code}
        problem.delete()
        return JsonResponse({'ok': True, 'deleted': deleted})
    title = str(payload.get('title') or problem.name).strip()
    if not title:
        return _json_error('Problem title is required.', 400)
    try:
        with transaction.atomic():
            problem.name = title[:100]
            if 'description' in payload:
                problem.description = str(payload.get('description') or '')
            if 'timeLimit' in payload or 'time_limit' in payload:
                problem.time_limit = _problem_time_seconds(payload.get('timeLimit', payload.get('time_limit')), problem.time_limit)
            if 'memoryLimit' in payload or 'memory_limit' in payload:
                problem.memory_limit = _problem_memory_kilobytes(payload.get('memoryLimit', payload.get('memory_limit')), problem.memory_limit)
            if 'visibility' in payload:
                problem.is_public = str(payload.get('visibility') or '').lower() == 'public'
            if 'scoringMode' in payload:
                problem.partial = str(payload.get('scoringMode') or '').lower() == 'partial'
            if 'rating' in payload:
                problem.points = max(0.0, float(payload.get('rating') or 0) * 100)
            problem.save()
            _apply_problem_languages_and_tags(problem, payload)
            _store_inline_problem_testcases(problem, payload.get('testCases'))
    except (TypeError, ValueError) as error:
        return _json_error(str(error), 400)
    return JsonResponse(_management_problem_row(problem), json_dumps_params={'ensure_ascii': False})


def _management_user_row(profile):
    row = _profile_row(profile, include_unlisted_organization=True)
    user = profile.user
    meta = _read_cppro_meta(profile)
    row.update({
        'id': profile.id,
        'user_id': user.id,
        'email': user.email or '',
        'full_name': user.get_full_name() or row.get('full_name') or user.username,
        'role': 'admin' if user.is_superuser else ('moderator' if user.is_staff else str(meta.get('role') or 'user')),
        'is_teacher': bool(meta.get('is_teacher') or user.is_staff),
        'membership_tier': str(meta.get('membership_tier') or 'free'),
        'membership_expires_at': meta.get('membership_expires_at'),
        'is_banned': not user.is_active,
        'ban_reason': profile.ban_reason or '',
        'active': bool(user.is_active),
    })
    return row


def _management_quiz_row(quiz):
    row = _quiz_row(quiz)
    row.update({
        'question_count': int(getattr(quiz, 'question_count', 0) or quiz.question_links.count()),
        'total_points': float(getattr(quiz, 'total_points', 0) or 0),
        'status': 'published' if quiz.is_public else 'draft',
        'is_public': bool(quiz.is_public),
        'shuffle_questions': bool(quiz.shuffle_questions),
        'created_at': quiz.created_at.isoformat() if quiz.created_at else '',
        'updated_at': quiz.updated_at.isoformat() if quiz.updated_at else '',
    })
    return row


def _management_question_row(question):
    return {
        'id': question.id,
        'code': question.code,
        'title': question.title,
        'prompt': question.content or '',
        'content': question.content or '',
        'question_type': question.type,
        'difficulty': question.level,
        'choices': question.choices or [],
        'correct_answers': question.correct_answers,
        'default_points': 1,
        'is_public': bool(question.is_public),
        'updated_at': question.updated_at.isoformat() if question.updated_at else '',
    }


def _management_badge_row(badge):
    return {
        'id': badge.id,
        'name': badge.name,
        'slug': _normalized_identifier(badge.name, badge.name),
        'description': '',
        'icon_url': badge.mini or '',
        'full_size': badge.full_size or '',
        'color': '#f59e0b',
        'background_color': '#fff7d6',
        'active': True,
        'sort_order': badge.id,
    }


def _management_dashboard_payload():
    Quiz = apps.get_model('quiz', 'Quiz')
    Judge = apps.get_model('judge', 'Judge')
    judge_fields = {field.name for field in Judge._meta.fields}
    online_filter = {'online': True} if 'online' in judge_fields else ({'is_disabled': False} if 'is_disabled' in judge_fields else {})
    recent = [
        _submission_row(submission)
        for submission in Submission.objects.select_related('problem', 'language', 'user__user', 'contest_object').order_by('-date', '-id')[:50]
    ]
    return {
        'counts': {
            'problems': Problem.objects.count(),
            'contests': Contest.objects.count(),
            'users': Profile.objects.count(),
            'submissions': Submission.objects.count(),
            'organizations': Organization.objects.count(),
            'quizzes': Quiz.objects.count(),
            'tickets': Ticket.objects.filter(is_open=True).count(),
        },
        'judge': {
            'total': Judge.objects.count(),
            'online': Judge.objects.filter(**online_filter).count() if online_filter else Judge.objects.count(),
            'running': Submission.objects.filter(status__in=['P', 'G']).count(),
            'queued': Submission.objects.filter(status__in=['QU', 'P']).count(),
        },
        'recent': {'rows': recent, 'total': Submission.objects.count()},
    }


def _management_rows(request, section):
    section = str(section or '').strip().lower().replace('_', '-')
    if section == 'dashboard':
        return _management_dashboard_payload()
    if section == 'analytics':
        cutoff = timezone.now() - timedelta(days=30)
        return {'rows': [
            {'metric': 'Submissions (30 days)', 'value': Submission.objects.filter(date__gte=cutoff).count()},
            {'metric': 'Accepted (30 days)', 'value': Submission.objects.filter(date__gte=cutoff, result='AC').count()},
            {'metric': 'Active users (30 days)', 'value': Profile.objects.filter(last_access__gte=cutoff).count()},
            {'metric': 'Published problems', 'value': Problem.objects.filter(is_public=True).count()},
        ]}
    if section in {'announcements', 'posts'}:
        post_kind = 'announcement' if section == 'announcements' else 'community'
        registered_ids = _registered_post_ids(post_kind)
        query = BlogPost.objects.filter(id__in=registered_ids).prefetch_related('authors__user').order_by('-publish_on', '-id')
        rows = []
        for post in query[:500]:
            row = _post_row(post, 'announcement' if section == 'announcements' else 'blog')
            row.update({
                'status': 'published' if post.visible else 'draft',
                'visible': bool(post.visible),
                'source': 'cppro-database',
            })
            rows.append(row)
        return {'rows': rows, 'total': query.count(), 'source': 'cppro-database'}
    if section == 'problems':
        query = Problem.objects.select_related('group').prefetch_related('types', 'allowed_languages').order_by('code')
        return {'rows': [_management_problem_row(problem) for problem in query[:1000]], 'total': query.count()}
    if section == 'problem-groups':
        rows = [{'id': item.id, 'name': item.name, 'full_name': item.full_name, 'description': '', 'sort_order': item.id} for item in ProblemGroup.objects.order_by('full_name', 'name')]
        return {'rows': rows, 'total': len(rows)}
    if section == 'tags':
        rows = [{'id': item.id, 'name': item.full_name or item.name, 'slug': item.name, 'color': '#f26f21'} for item in ProblemType.objects.order_by('full_name', 'name')]
        return {'rows': rows, 'total': len(rows)}
    if section == 'languages':
        runtime_by_language = {
            key: name for key, name in RuntimeVersion.objects.select_related('language').values_list('language__key', 'name')
        }
        rows = [{
            'id': item.id,
            'code': item.key,
            'key': item.key,
            'label': item.name,
            'name': item.name,
            'runtime_label': runtime_by_language.get(item.key) or '',
            'enabled': True,
            'sort_order': item.id,
        } for item in Language.objects.order_by('key')]
        return {'rows': rows, 'total': len(rows)}
    if section == 'users':
        query = Profile.objects.select_related('user').prefetch_related('badges', 'organizations').order_by('user__username')
        return {'rows': [_management_user_row(profile) for profile in query[:1000]], 'total': query.count()}
    if section == 'organizations':
        rows = [{
            'id': item.id,
            'slug': item.slug,
            'name': item.name,
            'description': item.about or '',
            'visibility': _organization_visibility(item),
            'member_count': item.members.count(),
            'admin_count': item.admins.count(),
        } for item in Organization.objects.prefetch_related('members', 'admins').order_by('name')]
        return {'rows': rows, 'total': len(rows)}
    if section == 'contests':
        contests = list(Contest.objects.prefetch_related('contest_problems__problem').order_by('-start_time', 'key')[:500])
        participation_counts, virtual_counts = _contest_participation_counts([item.id for item in contests])
        submission_counts = {}
        rows = [
            _contest_row(item, participation_counts, virtual_counts, submission_counts, _current_profile(request), request.user)
            for item in contests
        ]
        return {'rows': rows, 'total': Contest.objects.count()}
    if section == 'submissions':
        query = Submission.objects.select_related('problem', 'language', 'user__user', 'contest_object').order_by('-date', '-id')
        return {'rows': [_submission_row(item) for item in query[:500]], 'total': query.count()}
    if section == 'tickets':
        query = Ticket.objects.select_related('user__user').prefetch_related('assignees__user').order_by('-is_open', '-time')
        return {'rows': [_admin_ticket_row(item) for item in query[:500]], 'total': query.count()}
    if section == 'quizzes':
        Quiz = apps.get_model('quiz', 'Quiz')
        query = Quiz.objects.annotate(question_count=Count('question_links', distinct=True), total_points=Sum('question_links__points')).order_by('-created_at')
        return {'rows': [_management_quiz_row(item) for item in query[:500]], 'total': query.count()}
    if section == 'quiz-questions':
        Question = apps.get_model('quiz', 'QuizQuestion')
        query = Question.objects.order_by('-id')
        return {'rows': [_management_question_row(item) for item in query[:1000]], 'total': query.count()}
    if section == 'quiz-reviews':
        Answer = apps.get_model('quiz', 'QuizAnswer')
        query = Answer.objects.select_related('attempt__user__user', 'question').filter(question__type='sa').order_by('-saved_at')
        rows = [{
            'id': item.id,
            'answer_id': item.id,
            'question_title': item.question.title,
            'username': item.attempt.user.user.username,
            'text_answer': item.answer if isinstance(item.answer, str) else json.dumps(item.answer, ensure_ascii=False),
            'points_awarded': float(item.points or 0),
            'is_correct': bool(item.is_correct),
        } for item in query[:500]]
        return {'rows': rows, 'total': query.count()}
    if section == 'badges':
        Badge = apps.get_model('judge', 'Badge')
        rows = [_management_badge_row(item) for item in Badge.objects.order_by('name')]
        return {'rows': rows, 'total': len(rows)}
    if section in {'cluster', 'queue'}:
        Judge = apps.get_model('judge', 'Judge')
        rows = []
        for item in Judge.objects.order_by('name', 'id'):
            rows.append({
                'id': item.id,
                'name': getattr(item, 'name', '') or str(item),
                'online': bool(getattr(item, 'online', not getattr(item, 'is_disabled', False))),
                'is_disabled': bool(getattr(item, 'is_disabled', False)),
                'last_ip': str(getattr(item, 'last_ip', '') or ''),
            })
        return {'rows': rows, 'total': len(rows)}
    if section == 'rating':
        stored = _cppro_config_value(CPPRO_RATING_SETTINGS_KEY)
        return stored or {
            'enabled': False,
            'base_rating': 1500,
            'k_provisional': 40,
            'k_stable': 20,
            'provisional_contests': 8,
            'bands': [],
        }
    return {'rows': [], 'total': 0, 'message': 'This LCOJ module has no records yet.'}


def _create_management_post(request, payload, kind='community'):
    title = str(payload.get('title') or '').strip()
    content = str(payload.get('content') or '').strip()
    if len(title) < 3 or len(content) < 3:
        raise ValueError('Post title and content are required.')
    base = slugify(title)[:180] or 'post'
    slug = base
    suffix = 2
    while BlogPost.objects.filter(slug=slug).exists():
        slug = '%s-%s' % (base[:170], suffix)
        suffix += 1
    post = BlogPost.objects.create(
        title=title[:100],
        slug=slug,
        content=content,
        summary=str(payload.get('excerpt') or '')[:1000],
        og_image=str(payload.get('imageUrl') or '')[:150],
        visible=True,
        publish_on=timezone.now(),
        global_post=True,
    )
    if hasattr(request.user, 'profile'):
        post.authors.add(request.user.profile)
    _register_cppro_post(post, kind)
    return post


def _create_management_user(payload):
    User = get_user_model()
    username = str(payload.get('username') or '').strip()
    email = str(payload.get('email') or '').strip()
    password = str(payload.get('password') or '')
    if not re.match(r'^[A-Za-z0-9_]{3,30}$', username):
        raise ValueError('Username must contain 3-30 letters, numbers, or underscores.')
    if '@' not in email:
        raise ValueError('A valid email address is required.')
    if len(password) < 8:
        raise ValueError('Password must contain at least eight characters.')
    if User.objects.filter(username__iexact=username).exists():
        raise ValueError('Username already exists.')
    if User.objects.filter(email__iexact=email).exists():
        raise ValueError('Email already exists.')
    full_name = str(payload.get('fullName') or '').strip()
    names = full_name.split(None, 1)
    role = str(payload.get('role') or 'user').lower()
    user = User.objects.create_user(
        username=username,
        email=email,
        password=password,
        first_name=names[0][:30] if names else '',
        last_name=names[1][:150] if len(names) > 1 else '',
        is_staff=role in {'admin', 'moderator'},
        is_superuser=role == 'admin',
        is_active=True,
    )
    profile = getattr(user, 'profile', None)
    if profile is None:
        profile = Profile.objects.create(user=user, language=Language.get_default_language())
    meta = _read_cppro_meta(profile)
    meta.update({
        'role': role,
        'is_teacher': bool(payload.get('isTeacher')),
        'membership_tier': str(payload.get('membershipTier') or 'free'),
        'membership_expires_at': payload.get('membershipExpiresAt') or None,
    })
    _write_cppro_meta(profile, meta)
    return profile


def _update_management_user(profile, payload):
    user = profile.user
    if 'isBanned' in payload or 'is_banned' in payload:
        banned = _admin_boolean(payload.get('isBanned', payload.get('is_banned')))
        user.is_active = not banned
        profile.ban_reason = str(payload.get('banReason', payload.get('ban_reason')) or '') if banned else ''
        profile.save(update_fields=['ban_reason'])
    role = str(payload.get('role') or '').lower()
    if role:
        user.is_superuser = role == 'admin'
        user.is_staff = role in {'admin', 'moderator'}
    user.save()
    meta = _read_cppro_meta(profile)
    if role:
        meta['role'] = role
    if 'isTeacher' in payload or 'is_teacher' in payload:
        meta['is_teacher'] = _admin_boolean(payload.get('isTeacher', payload.get('is_teacher')))
    if 'membershipTier' in payload or 'membership_tier' in payload:
        meta['membership_tier'] = str(payload.get('membershipTier', payload.get('membership_tier')) or 'free')
    _write_cppro_meta(profile, meta)
    return profile


def _quiz_question_type(value):
    mapping = {
        'single_choice': 'mc',
        'multiple_choice': 'ma',
        'multiple_answer': 'ma',
        'true_false': 'tf',
        'short_answer': 'sa',
        'mc': 'mc', 'ma': 'ma', 'tf': 'tf', 'sa': 'sa',
    }
    return mapping.get(str(value or '').lower(), 'mc')


def _create_management_question(request, payload):
    Question = apps.get_model('quiz', 'QuizQuestion')
    title = str(payload.get('title') or '').strip()
    content = str(payload.get('prompt') or payload.get('content') or '').strip()
    if len(title) < 3 or not content:
        raise ValueError('Question title and prompt are required.')
    question_type = _quiz_question_type(payload.get('questionType', payload.get('question_type')))
    raw_options = payload.get('options') or []
    choices = [str(item.get('content') or '') for item in raw_options if isinstance(item, dict) and str(item.get('content') or '').strip()]
    correct = [index for index, item in enumerate(raw_options) if isinstance(item, dict) and item.get('isCorrect')]
    if question_type in {'mc', 'tf'}:
        correct_answers = correct[0] if correct else 0
    elif question_type == 'ma':
        correct_answers = correct
    else:
        correct_answers = payload.get('correctAnswers') or []
    question = Question.objects.create(
        code=_unique_model_code(Question, payload.get('code'), title),
        type=question_type,
        title=title[:200],
        content=content,
        choices=choices,
        correct_answers=correct_answers,
        level=str(payload.get('difficulty') or 'easy').lower(),
        is_public=True,
    )
    if hasattr(request.user, 'profile'):
        question.authors.add(request.user.profile)
    return question


def _create_management_quiz(request, payload):
    Quiz = apps.get_model('quiz', 'Quiz')
    Link = apps.get_model('quiz', 'QuizQuestionLink')
    Question = apps.get_model('quiz', 'QuizQuestion')
    title = str(payload.get('title') or '').strip()
    if len(title) < 3:
        raise ValueError('Quiz title must contain at least three characters.')
    quiz = Quiz.objects.create(
        code=_unique_model_code(Quiz, payload.get('slug'), title),
        name=title[:100],
        description=str(payload.get('description') or ''),
        time_limit=max(1, int(payload.get('timeLimitMinutes') or 30)),
        max_attempts=max(1, int(payload.get('attemptLimit') or 1)),
        shuffle_questions=bool(payload.get('shuffleQuestions')),
        is_public=str(payload.get('status') or 'draft').lower() == 'published',
    )
    if hasattr(request.user, 'profile'):
        quiz.authors.add(request.user.profile)
    raw_items = payload.get('items') or []
    question_ids = [int(item.get('questionId')) for item in raw_items if isinstance(item, dict) and str(item.get('questionId') or '').isdigit()]
    questions = {item.id: item for item in Question.objects.filter(id__in=question_ids)}
    Link.objects.bulk_create([
        Link(quiz=quiz, question=questions[question_id], points=1, order=index)
        for index, question_id in enumerate(question_ids, start=1)
        if question_id in questions
    ])
    return quiz


@csrf_protect
@require_http_methods(['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
def cppro_admin_management(request, section, identifier=None):
    denied = _require_platform_admin(request)
    if denied:
        return denied
    section = str(section or '').strip().lower().replace('_', '-')
    if request.method == 'GET':
        return JsonResponse(_management_rows(request, section), json_dumps_params={'ensure_ascii': False})
    payload = _admin_payload(request)
    try:
        if section in {'announcements', 'posts'}:
            if request.method == 'POST':
                post = _create_management_post(
                    request,
                    payload,
                    kind='announcement' if section == 'announcements' else 'community',
                )
            else:
                allowed_ids = _registered_post_ids('announcement' if section == 'announcements' else 'community')
                post = BlogPost.objects.filter(id=int(identifier), id__in=allowed_ids).first() if str(identifier).isdigit() else None
                if not post:
                    return _json_error('Post not found.', 404)
                if request.method == 'DELETE':
                    _unregister_cppro_post(post)
                    post.delete()
                    return JsonResponse({'ok': True})
                post.title = str(payload.get('title') or post.title)[:100]
                post.content = str(payload.get('content') if 'content' in payload else post.content)
                post.summary = str(payload.get('excerpt') if 'excerpt' in payload else post.summary)[:1000]
                post.og_image = str(payload.get('imageUrl') if 'imageUrl' in payload else post.og_image)[:150]
                post.visible = str(payload.get('status') or 'published').lower() != 'draft'
                post.save()
            return JsonResponse(_post_row(post), status=201 if request.method == 'POST' else 200, json_dumps_params={'ensure_ascii': False})

        if section == 'users':
            if request.method == 'POST':
                profile = _create_management_user(payload)
                return JsonResponse(_management_user_row(profile), status=201, json_dumps_params={'ensure_ascii': False})
            profile = Profile.objects.select_related('user').filter(id=int(identifier)).first() if str(identifier).isdigit() else None
            if profile is None:
                return _json_error('User not found.', 404)
            if request.method == 'DELETE':
                if profile.user_id == request.user.id or profile.user.is_superuser:
                    return _json_error('The active administrator account cannot be deleted.', 409)
                profile.user.delete()
                return JsonResponse({'ok': True})
            profile = _update_management_user(profile, payload)
            return JsonResponse(_management_user_row(profile), json_dumps_params={'ensure_ascii': False})

        if section == 'badges':
            Badge = apps.get_model('judge', 'Badge')
            if request.method == 'POST':
                name = str(payload.get('name') or '').strip()
                if len(name) < 2:
                    raise ValueError('Badge name is required.')
                badge = Badge.objects.create(name=name[:50], mini=str(payload.get('iconUrl') or 'badge')[:150], full_size=str(payload.get('fullSize') or payload.get('iconUrl') or 'badge')[:150])
                return JsonResponse(_management_badge_row(badge), status=201, json_dumps_params={'ensure_ascii': False})
            badge = Badge.objects.filter(id=int(identifier)).first() if str(identifier).isdigit() else None
            if not badge:
                return _json_error('Badge not found.', 404)
            if request.method == 'DELETE':
                badge.delete()
                return JsonResponse({'ok': True})
            badge.name = str(payload.get('name') or badge.name)[:50]
            badge.mini = str(payload.get('iconUrl') or badge.mini or 'badge')[:150]
            badge.full_size = str(payload.get('fullSize') or payload.get('iconUrl') or badge.full_size or badge.mini)[:150]
            badge.save()
            return JsonResponse(_management_badge_row(badge), json_dumps_params={'ensure_ascii': False})

        if section == 'quiz-questions':
            Question = apps.get_model('quiz', 'QuizQuestion')
            if request.method == 'POST':
                question = _create_management_question(request, payload)
                return JsonResponse(_management_question_row(question), status=201, json_dumps_params={'ensure_ascii': False})
            question = Question.objects.filter(id=int(identifier)).first() if str(identifier).isdigit() else None
            if not question:
                return _json_error('Quiz question not found.', 404)
            if request.method == 'DELETE':
                question.delete()
                return JsonResponse({'ok': True})
            return _json_error('Question editing is not available from this compact form yet.', 405)

        if section == 'quizzes':
            Quiz = apps.get_model('quiz', 'Quiz')
            if request.method == 'POST':
                quiz = _create_management_quiz(request, payload)
                return JsonResponse(_management_quiz_row(quiz), status=201, json_dumps_params={'ensure_ascii': False})
            quiz = Quiz.objects.filter(id=int(identifier)).first() if str(identifier).isdigit() else None
            if not quiz:
                return _json_error('Quiz not found.', 404)
            if request.method == 'DELETE':
                quiz.delete()
                return JsonResponse({'ok': True})
            quiz.name = str(payload.get('title') or quiz.name)[:100]
            quiz.description = str(payload.get('description') if 'description' in payload else quiz.description)
            if 'status' in payload:
                quiz.is_public = str(payload.get('status')).lower() == 'published'
            quiz.save()
            return JsonResponse(_management_quiz_row(quiz), json_dumps_params={'ensure_ascii': False})

        if section == 'quiz-reviews':
            Answer = apps.get_model('quiz', 'QuizAnswer')
            answer = Answer.objects.filter(id=int(identifier)).first() if str(identifier).isdigit() else None
            if not answer:
                return _json_error('Quiz answer not found.', 404)
            answer.points = max(0.0, float(payload.get('pointsAwarded') or 0))
            answer.is_correct = answer.points > 0
            answer.save(update_fields=['points', 'is_correct'])
            return JsonResponse({'ok': True, 'id': answer.id, 'points_awarded': answer.points})

        if section == 'rating':
            if request.method not in {'PUT', 'PATCH'}:
                return _json_error('Use PUT to save rating settings.', 405)
            _save_cppro_config_value(CPPRO_RATING_SETTINGS_KEY, payload)
            return JsonResponse(payload, json_dumps_params={'ensure_ascii': False})

        if section == 'tags':
            if request.method == 'POST':
                name = str(payload.get('name') or '').strip()
                if not name:
                    raise ValueError('Tag name is required.')
                item = ProblemType.objects.create(name=_normalized_identifier(name, name, maximum=20), full_name=name[:100])
            else:
                item = ProblemType.objects.filter(id=int(identifier)).first() if str(identifier).isdigit() else None
                if not item:
                    return _json_error('Tag not found.', 404)
                if request.method == 'DELETE':
                    item.delete()
                    return JsonResponse({'ok': True})
                item.full_name = str(payload.get('name') or item.full_name)[:100]
                item.save(update_fields=['full_name'])
            return JsonResponse({'id': item.id, 'name': item.full_name, 'slug': item.name}, status=201 if request.method == 'POST' else 200)

        if section == 'problem-groups':
            if request.method == 'POST':
                name = str(payload.get('name') or '').strip()
                if not name:
                    raise ValueError('Group name is required.')
                item = ProblemGroup.objects.create(name=_normalized_identifier(name, name, maximum=20), full_name=name[:100])
            else:
                item = ProblemGroup.objects.filter(id=int(identifier)).first() if str(identifier).isdigit() else None
                if not item:
                    return _json_error('Problem group not found.', 404)
                if request.method == 'DELETE':
                    if Problem.objects.filter(group=item).exists():
                        return _json_error('Move problems out of this group before deleting it.', 409)
                    item.delete()
                    return JsonResponse({'ok': True})
                item.full_name = str(payload.get('name') or item.full_name)[:100]
                item.save(update_fields=['full_name'])
            return JsonResponse({'id': item.id, 'name': item.name, 'full_name': item.full_name}, status=201 if request.method == 'POST' else 200)
    except (IntegrityError, TypeError, ValueError) as error:
        return _json_error(str(error), 400)
    return _json_error('This management operation is not supported.', 405)


def _find_blog_post(request, identifier):
    community_ids = _registered_post_ids('community')
    post = BlogPost.objects.filter(id=int(identifier), id__in=community_ids).first() if str(identifier).isdigit() else None
    if post is None:
        post = BlogPost.objects.filter(slug=str(identifier), id__in=community_ids).first()
    return post if post and post.can_see(request.user) else None


def _post_comment_config_key(post):
    return 'cppro_post_comments_%s' % post.id


def _post_comment_rows(post):
    value = _cppro_config_value(_post_comment_config_key(post)).get('rows', [])
    return value if isinstance(value, list) else []


def _save_post_comment_rows(post, rows):
    _save_cppro_config_value(_post_comment_config_key(post), {'rows': rows[-1000:]})


@csrf_protect
@require_http_methods(['GET', 'POST'])
def cppro_post_social(request, post_id, resource):
    post = _find_blog_post(request, post_id)
    if not post:
        return _json_error('Post not found.', 404)
    resource = str(resource or '').strip().lower()
    if resource in {'votes', 'vote'}:
        my_vote = 0
        if request.method == 'POST':
            if not request.user.is_authenticated:
                return _json_error('Login required.', 401)
            payload = _admin_payload(request)
            my_vote = 1 if int(payload.get('voteType') or 0) > 0 else 0
        return JsonResponse({'totalVotes': int(post.score or 0), 'postVotes': int(post.score or 0), 'myVote': my_vote})
    if resource in {'reactions', 'reaction'}:
        payload = _admin_payload(request) if request.method == 'POST' else {}
        reaction = str(payload.get('reaction') or '').strip() or None
        return JsonResponse({
            'reactions': {reaction: 1} if reaction else {},
            'reactionCount': 1 if reaction else 0,
            'myReaction': reaction,
            'reactors': [],
        }, json_dumps_params={'ensure_ascii': False})
    return _json_error('Unknown social resource.', 404)


@csrf_protect
@require_http_methods(['GET', 'POST'])
def cppro_post_comments(request, identifier, comment_id=None, action=None):
    post = _find_blog_post(request, identifier)
    if not post:
        return _json_error('Post not found.', 404)
    rows = _post_comment_rows(post)
    if request.method == 'GET':
        return JsonResponse({'rows': rows, 'total': len(rows)}, json_dumps_params={'ensure_ascii': False})
    if not request.user.is_authenticated:
        return _json_error('Login required.', 401)
    payload = _admin_payload(request)
    if comment_id is None:
        profile = _current_profile(request)
        body = str(payload.get('body') or '').strip()
        if not body:
            return _json_error('Comment body is required.', 400)
        next_id = max([int(item.get('id') or 0) for item in rows if isinstance(item, dict)] or [0]) + 1
        row = {
            'id': next_id,
            'parent_id': payload.get('parentId'),
            'author_username': request.user.username,
            'author_full_name': _author_name(profile) if profile else request.user.username,
            'author_avatar_url': _profile_avatar(profile) if profile else '',
            'author_role': 'Admin' if request.user.is_staff else 'Member',
            'body': body[:20000],
            'score': 0,
            'vote_count': 0,
            'my_vote': 0,
            'reactions': {},
            'reaction_count': 0,
            'my_reaction': None,
            'created_at': timezone.now().isoformat(),
        }
        rows.append(row)
        _save_post_comment_rows(post, rows)
        return JsonResponse(row, status=201, json_dumps_params={'ensure_ascii': False})
    row = next((item for item in rows if isinstance(item, dict) and str(item.get('id')) == str(comment_id)), None)
    if row is None:
        return _json_error('Comment not found.', 404)
    action = str(action or '').strip().lower()
    if action == 'vote':
        vote = int(payload.get('voteType') or 0)
        row['my_vote'] = max(-1, min(1, vote))
        row['score'] = row['my_vote']
        row['vote_count'] = 1 if row['my_vote'] else 0
    elif action == 'reaction':
        reaction = str(payload.get('reaction') or '').strip() or None
        row['my_reaction'] = reaction
        row['reactions'] = {reaction: 1} if reaction else {}
        row['reaction_count'] = 1 if reaction else 0
    else:
        return _json_error('Unknown comment action.', 404)
    _save_post_comment_rows(post, rows)
    return JsonResponse(row, json_dumps_params={'ensure_ascii': False})
