from .models import MemberProfile


def member_context(request):
    if not request.user.is_authenticated:
        return {"current_member": None}
    try:
        return {"current_member": request.user.member_profile}
    except MemberProfile.DoesNotExist:
        return {"current_member": None}
