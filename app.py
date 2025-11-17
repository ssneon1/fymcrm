from flask import Flask, render_template, request, jsonify, session, send_file
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from datetime import datetime, timedelta
import json
import os
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from flask_socketio import SocketIO, emit, join_room
from werkzeug.utils import secure_filename
import pandas as pd
import io

app = Flask(__name__)

# Load environment variables (optional)
load_dotenv()

mysql_default_url = "sqlite:///app.db"
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', mysql_default_url)


app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'taskflow-secret-key-2024')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', mysql_default_url)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['ALLOWED_EXTENSIONS'] = {'xlsx', 'xls'}

# Create uploads directory if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Initialize Socket.IO for real-time communication
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Database Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # admin, manager, staff
    email = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Service(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, default=0.0)
    fee = db.Column(db.Float, default=0.0)
    charge = db.Column(db.Float, default=0.0)
    link = db.Column(db.String(200))
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(50), unique=True, nullable=False)
    customer_name = db.Column(db.String(100), nullable=False)
    contact_number = db.Column(db.String(20), nullable=False)
    service_type = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='Received')  # Received, Pending, In Progress, Completed, Hold, Cancelled
    assigned_to = db.Column(db.String(100), nullable=False)
    branch_code = db.Column(db.String(50), nullable=False)
    paymode = db.Column(db.String(20), default='Cash')
    service_price = db.Column(db.Float, default=0.0)
    paid_amount = db.Column(db.Float, default=0.0)
    service_charge = db.Column(db.Float, default=0.0)
    description = db.Column(db.Text)
    edited = db.Column(db.Boolean, default=False)
    edit_reason = db.Column(db.Text)
    task_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # For task sharing - store as JSON string
    shared_with = db.Column(db.Text, default='[]')
    
    # New fields for payment tracking
    final_payment_amount = db.Column(db.Float, default=0.0)
    final_paymode = db.Column(db.String(20))
    payment_notes = db.Column(db.Text)
    
    def get_shared_with(self):
        if not self.shared_with or self.shared_with.strip() == '':
            return []
        try:
            result = json.loads(self.shared_with)
            return result if isinstance(result, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    
    def set_shared_with(self, staff_list):
        if not isinstance(staff_list, list):
            staff_list = []
        self.shared_with = json.dumps(staff_list)
    
    def is_completed(self):
        return self.status == 'Completed'
    
    def get_due_amount(self):
        return max(0, self.service_price - self.paid_amount)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Real-time event handlers
@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")
    emit('connected', {'message': 'Connected to real-time updates'})

@socketio.on('disconnect')
def handle_disconnect():
    print(f"Client disconnected: {request.sid}")

@socketio.on('join_room')
def handle_join_room(data):
    room = data.get('room', 'global')
    join_room(room)
    emit('room_joined', {'room': room, 'message': f'Joined room: {room}'})

# Function to broadcast task updates to all connected clients
def broadcast_task_update(event_type, task_data):
    socketio.emit('task_updated', {
        'type': event_type,
        'task': task_data,
        'timestamp': datetime.utcnow().isoformat()
    })

# Function to broadcast dashboard updates
def broadcast_dashboard_update():
    stats = get_dashboard_stats()
    socketio.emit('dashboard_updated', {
        'stats': stats,
        'timestamp': datetime.utcnow().isoformat()
    })

# Function to get dashboard stats (reusable)
def get_dashboard_stats():
    total_tasks = Task.query.count()
    today = datetime.now().date()
    tasks_today = Task.query.filter(Task.task_date == today).count()
    completed_tasks = Task.query.filter_by(status='Completed').count()
    total_revenue = db.session.query(db.func.sum(Task.paid_amount)).scalar() or 0
    overdue_threshold = datetime.now() - timedelta(hours=24)
    overdue_tasks = Task.query.filter(
        Task.status.in_(['Pending', 'In Progress', 'Hold']),
        Task.created_at < overdue_threshold
    ).count()

    return {
        'total_tasks': total_tasks,
        'tasks_today': tasks_today,
        'completed_tasks': completed_tasks,
        'total_revenue': total_revenue,
        'overdue_tasks': overdue_tasks
    }

def init_db():
    """Initialize the database with default data"""
    # Check if users already exist
    if not User.query.first():
        print("Initializing database with default data...")

        # Create default users
        users_data = [
            {'username': 'admin', 'role': 'admin', 'email': 'admin@taskflow.com'},
            {'username': 'manager', 'role': 'manager', 'email': 'manager@taskflow.com'},
            {'username': 'staff1', 'role': 'staff', 'email': 'staff1@taskflow.com'},
            {'username': 'staff2', 'role': 'staff', 'email': 'staff2@taskflow.com'},
            {'username': 'staff3', 'role': 'staff', 'email': 'staff3@taskflow.com'},
        ]

        for user_data in users_data:
            user = User(
                username=user_data['username'],
                role=user_data['role'],
                email=user_data['email']
            )
            if user_data['username'] == 'admin':
                user.set_password('admin123')
            elif user_data['username'] == 'manager':
                user.set_password('manager123')
            else:
                user.set_password('password123')
            db.session.add(user)

        # Create default services
        services_data = [
            {'name': 'Consultation', 'price': 1500, 'fee': 100, 'charge': 100,
             'link': 'https://example.com/consultation', 'note': 'Initial consultation for new clients'},
            {'name': 'Repair', 'price': 2000, 'fee': 150, 'charge': 150,
             'link': 'https://example.com/repair', 'note': 'Device repair service with 30-day warranty'},
            {'name': 'Sales', 'price': 500, 'fee': 50, 'charge': 50,
             'link': 'https://example.com/sales', 'note': 'Product sales and inquiry service'},
            {'name': 'Support', 'price': 800, 'fee': 80, 'charge': 80,
             'link': 'https://example.com/support', 'note': 'Technical support and troubleshooting'}
        ]

        for service_data in services_data:
            service = Service(**service_data)
            db.session.add(service)

        # Create sample tasks
        sample_tasks = [
            {
                'order_no': 'TF-001',
                'customer_name': 'Michael Brown',
                'contact_number': '555-1234',
                'service_type': 'Consultation',
                'status': 'Completed',
                'assigned_to': 'staff1',
                'branch_code': 'BANK ROAD',
                'paymode': 'Credit Card',
                'service_price': 1500,
                'paid_amount': 1500,
                'service_charge': 100,
                'description': 'Initial business consultation',
                'task_date': datetime.now().date()
            },
            {
                'order_no': 'TF-002',
                'customer_name': 'Sarah Johnson',
                'contact_number': '555-5678',
                'service_type': 'Repair',
                'status': 'In Progress',
                'assigned_to': 'staff2',
                'branch_code': 'UNIVERSITY ROAD',
                'paymode': 'Cash',
                'service_price': 2000,
                'paid_amount': 1000,
                'service_charge': 150,
                'description': 'Device repair service',
                'task_date': datetime.now().date()
            },
            {
                'order_no': 'TF-003',
                'customer_name': 'Robert Wilson',
                'contact_number': '555-9012',
                'service_type': 'Sales',
                'status': 'Pending',
                'assigned_to': 'staff3',
                'branch_code': 'BANK ROAD',
                'paymode': 'UPI',
                'service_price': 500,
                'paid_amount': 500,
                'service_charge': 50,
                'description': 'Product purchase inquiry',
                'task_date': datetime.now().date()
            }
        ]

        for task_data in sample_tasks:
            task = Task(**task_data)
            db.session.add(task)

        db.session.commit()
        print("Database initialized with default data")
    else:
        print("Database already contains data")

# Initialize database
with app.app_context():
    db.create_all()
    init_db()

# Utility functions
def get_staff_list():
    return [user.username for user in User.query.filter_by(role='staff').all()]

def generate_order_no(branch_code):
    # Define branch prefixes
    branch_prefixes = {
        'BANK ROAD': 'BR',
        'UNIVERSITY ROAD': 'UR'
    }
    
    # Get the appropriate prefix for the branch, default to 'TF' if not found
    prefix = branch_prefixes.get(branch_code.upper(), 'TF')
    
    # Get current date in DDMMYY format
    current_date = datetime.now().strftime('%d%m%y')
    
    # Find the last task for this specific branch and date
    last_task = Task.query.filter(
        Task.order_no.like(f'{prefix}-{current_date}-%')
    ).order_by(Task.id.desc()).first()
    
    if last_task:
        try:
            # Extract the order number part (after the date)
            last_number = int(last_task.order_no.split('-')[2])
            return f"{prefix}-{current_date}-{last_number + 1:02d}"
        except (IndexError, ValueError):
            return f"{prefix}-{current_date}-01"
    else:
        return f"{prefix}-{current_date}-01"

# Authentication routes
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    user = User.query.filter_by(username=username).first()

    if user and user.check_password(password):
        login_user(user)
        return jsonify({
            'success': True,
            'message': 'Login successful',
            'user': {
                'id': user.id,
                'username': user.username,
                'role': user.role,
                'email': user.email
            }
        })
    else:
        return jsonify({
            'success': False,
            'message': 'Invalid credentials'
        }), 401

@app.route('/api/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({'success': True, 'message': 'Logout successful'})

@app.route('/api/current-user')
@login_required
def current_user_info():
    return jsonify({
        'user': {
            'id': current_user.id,
            'username': current_user.username,
            'role': current_user.role,
            'email': current_user.email
        }
    })

# User management routes
@app.route('/api/users')
@login_required
def get_users():
    if current_user.role not in ['admin', 'manager']:
        return jsonify({'error': 'Access denied'}), 403

    users = User.query.all()
    users_data = []
    for user in users:
        users_data.append({
            'id': user.id,
            'username': user.username,
            'role': user.role,
            'email': user.email,
            'created_at': user.created_at.isoformat() if user.created_at else None
        })

    return jsonify(users_data)

@app.route('/api/users', methods=['POST'])
@login_required
def create_user():
    if current_user.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403

    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    role = data.get('role')
    email = data.get('email')

    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Username already exists'}), 400

    user = User(username=username, role=role, email=email)
    user.set_password(password)

    try:
        db.session.add(user)
        db.session.commit()
        return jsonify({'success': True, 'message': 'User created successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to create user'}), 500

@app.route('/api/users/<int:user_id>', methods=['PUT'])
@login_required
def update_user(user_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403

    user = User.query.get_or_404(user_id)
    data = request.get_json()

    if 'username' in data:
        # Check if new username already exists (excluding current user)
        existing_user = User.query.filter(User.username == data['username'], User.id != user_id).first()
        if existing_user:
            return jsonify({'error': 'Username already exists'}), 400
        user.username = data['username']
    
    if 'email' in data:
        user.email = data['email']
    
    if 'role' in data:
        user.role = data['role']
    
    if 'password' in data and data['password']:
        user.set_password(data['password'])

    try:
        db.session.commit()
        return jsonify({'success': True, 'message': 'User updated successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to update user'}), 500

@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@login_required
def delete_user(user_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403

    user = User.query.get_or_404(user_id)

    # Prevent deleting yourself
    if user.id == current_user.id:
        return jsonify({'error': 'Cannot delete your own account'}), 400

    try:
        db.session.delete(user)
        db.session.commit()
        return jsonify({'success': True, 'message': 'User deleted successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to delete user'}), 500

# Branch management routes
@app.route('/api/branches')
@login_required
def get_branches():
    # Return predefined branches - in production, you might want to store these in database
    branches = ['BANK ROAD', 'UNIVERSITY ROAD']
    return jsonify(branches)

@app.route('/api/branches', methods=['POST'])
@login_required
def create_branch():
    if current_user.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403
    
    data = request.get_json()
    branch_name = data.get('name')
    
    # In a real application, you would save this to a database table
    # For now, we'll just return success
    return jsonify({'success': True, 'message': 'Branch added successfully'})

# Service management routes
@app.route('/api/services')
@login_required
def get_services():
    try:
        services = Service.query.all()
        services_data = []
        for service in services:
            services_data.append({
                'id': service.id,
                'name': service.name,
                'price': service.price,
                'fee': service.fee,
                'charge': service.charge,
                'link': service.link,
                'note': service.note,
                'created_at': service.created_at.isoformat() if service.created_at else None
            })

        return jsonify(services_data)
    except Exception as e:
        return jsonify({'error': 'Database error'}), 500

@app.route('/api/services', methods=['POST'])
@login_required
def create_service():
    if current_user.role == 'staff':
        return jsonify({'error': 'Access denied'}), 403

    data = request.get_json()
    service = Service(
        name=data.get('name'),
        price=data.get('price', 0),
        fee=data.get('fee', 0),
        charge=data.get('charge', 0),
        link=data.get('link', ''),
        note=data.get('note', '')
    )

    try:
        db.session.add(service)
        db.session.commit()
        # Broadcast service update
        socketio.emit('service_updated', {
            'type': 'created',
            'service': {
                'id': service.id,
                'name': service.name,
                'price': service.price,
                'fee': service.fee,
                'charge': service.charge,
                'link': service.link,
                'note': service.note
            }
        })
        return jsonify({'success': True, 'message': 'Service created successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to create service'}), 500

@app.route('/api/services/<int:service_id>', methods=['PUT'])
@login_required
def update_service(service_id):
    if current_user.role == 'staff':
        return jsonify({'error': 'Access denied'}), 403

    service = Service.query.get_or_404(service_id)
    data = request.get_json()

    service.name = data.get('name', service.name)
    service.price = data.get('price', service.price)
    service.fee = data.get('fee', service.fee)
    service.charge = data.get('charge', service.charge)
    service.link = data.get('link', service.link)
    service.note = data.get('note', service.note)

    try:
        db.session.commit()
        # Broadcast service update
        socketio.emit('service_updated', {
            'type': 'updated',
            'service': {
                'id': service.id,
                'name': service.name,
                'price': service.price,
                'fee': service.fee,
                'charge': service.charge,
                'link': service.link,
                'note': service.note
            }
        })
        return jsonify({'success': True, 'message': 'Service updated successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to update service'}), 500

@app.route('/api/services/<int:service_id>', methods=['DELETE'])
@login_required
def delete_service(service_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Admin access required'}), 403

    service = Service.query.get_or_404(service_id)

    try:
        db.session.delete(service)
        db.session.commit()
        # Broadcast service update
        socketio.emit('service_updated', {
            'type': 'deleted',
            'service_id': service_id
        })
        return jsonify({'success': True, 'message': 'Service deleted successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to delete service'}), 500

# Task management routes
@app.route('/api/tasks')
@login_required
def get_tasks():
    try:
        # Get filter parameters
        date_filter = request.args.get('date', 'all')
        branch_filter = request.args.get('branch', 'all')
        staff_filter = request.args.get('staff', 'all')
        status_filter = request.args.get('status', 'all')
        service_filter = request.args.get('service', 'all')
        search_term = request.args.get('search', '')

        # Start with base query
        query = Task.query

        # Apply filters
        if date_filter != 'all':
            today = datetime.now().date()
            if date_filter == 'today':
                query = query.filter(Task.task_date == today)
            elif date_filter == 'yesterday':
                query = query.filter(Task.task_date == today - timedelta(days=1))
            elif date_filter == 'tomorrow':
                query = query.filter(Task.task_date == today + timedelta(days=1))
            elif date_filter == 'last30':
                query = query.filter(Task.task_date >= today - timedelta(days=30))

        if branch_filter != 'all':
            query = query.filter(Task.branch_code == branch_filter)

        if staff_filter != 'all':
            query = query.filter(Task.assigned_to == staff_filter)

        if status_filter != 'all':
            query = query.filter(Task.status == status_filter)

        if service_filter != 'all':
            query = query.filter(Task.service_type == service_filter)

        if search_term:
            query = query.filter(
                (Task.order_no.ilike(f'%{search_term}%')) |
                (Task.customer_name.ilike(f'%{search_term}%')) |
                (Task.contact_number.ilike(f'%{search_term}%'))
            )

        # For staff panel, show all tasks to everyone
        # Regular tasks page maintains role-based filtering
        if request.args.get('panel') != 'staff':
            if current_user.role == 'staff':
                query = query.filter(
                    (Task.assigned_to == current_user.username) |
                    (Task.shared_with.contains(f'"{current_user.username}"'))
                )

        tasks = query.order_by(Task.created_at.desc()).all()

        tasks_data = []
        for task in tasks:
            tasks_data.append({
                'id': task.id,
                'order_no': task.order_no,
                'customer_name': task.customer_name,
                'contact_number': task.contact_number,
                'service_type': task.service_type,
                'status': task.status,
                'assigned_to': task.assigned_to,
                'branch_code': task.branch_code,
                'paymode': task.paymode,
                'service_price': task.service_price,
                'paid_amount': task.paid_amount,
                'service_charge': task.service_charge,
                'description': task.description,
                'edited': task.edited,
                'edit_reason': task.edit_reason,
                'shared_with': task.get_shared_with(),
                'task_date': task.task_date.isoformat() if task.task_date else None,
                'created_at': task.created_at.isoformat() if task.created_at else None,
                'updated_at': task.updated_at.isoformat() if task.updated_at else None,
                'due_amount': task.get_due_amount(),
                'final_payment_amount': task.final_payment_amount,
                'final_paymode': task.final_paymode,
                'payment_notes': task.payment_notes
            })

        return jsonify(tasks_data)
    except Exception as e:
        print(f"Error fetching tasks: {e}")
        return jsonify({'error': 'Database error'}), 500

@app.route('/api/tasks', methods=['POST'])
@login_required
def create_task():
    data = request.get_json()

    # Validate required fields
    required_fields = ['customer_name', 'contact_number', 'service_type', 'assigned_to', 'branch_code']
    for field in required_fields:
        if not data.get(field):
            return jsonify({'error': f'Missing required field: {field}'}), 400

    task = Task(
        order_no=generate_order_no(data.get('branch_code')),
        customer_name=data.get('customer_name'),
        contact_number=data.get('contact_number'),
        service_type=data.get('service_type'),
        assigned_to=data.get('assigned_to'),
        branch_code=data.get('branch_code'),
        paymode=data.get('paymode', 'Cash'),
        service_price=data.get('service_price', 0),
        paid_amount=data.get('paid_amount', 0),
        service_charge=data.get('service_charge', 0),
        description=data.get('description', ''),
        task_date=datetime.now().date()
    )

    try:
        db.session.add(task)
        db.session.commit()
        
        # Prepare task data for broadcasting
        task_data = {
            'id': task.id,
            'order_no': task.order_no,
            'customer_name': task.customer_name,
            'service_type': task.service_type,
            'status': task.status,
            'assigned_to': task.assigned_to,
            'branch_code': task.branch_code
        }
        
        # Broadcast task creation
        broadcast_task_update('created', task_data)
        # Broadcast dashboard update
        broadcast_dashboard_update()
        
        return jsonify({'success': True, 'message': 'Task created successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to create task'}), 500

@app.route('/api/tasks/<int:task_id>', methods=['PUT'])
@login_required
def update_task(task_id):
    task = Task.query.get_or_404(task_id)
    data = request.get_json()

    # Check if task is completed
    is_completed = task.is_completed()

    # Staff permissions
    if current_user.role == 'staff':
        # Check if staff is allowed to edit this task
        if task.assigned_to != current_user.username and current_user.username not in task.get_shared_with():
            return jsonify({'error': 'You can only edit tasks assigned to you or shared with you'}), 403
        
        # If task is completed, staff cannot edit anything
        if is_completed:
            return jsonify({'error': 'Cannot edit completed orders'}), 403
        
        # Staff can update these fields before completion
        allowed_fields = ['status', 'description', 'paid_amount', 'service_charge']
        
        # Track if any changes were made
        changes_made = False
        edit_reason_parts = []
        
        for field in allowed_fields:
            if field in data and getattr(task, field) != data[field]:
                old_value = getattr(task, field)
                new_value = data[field]
                setattr(task, field, new_value)
                changes_made = True
                edit_reason_parts.append(f"{field} changed from {old_value} to {new_value}")
        
        # If changes were made, mark as edited and record reason
        if changes_made:
            task.edited = True
            if data.get('edit_reason'):
                task.edit_reason = data.get('edit_reason')
            elif edit_reason_parts:
                task.edit_reason = "; ".join(edit_reason_parts)
        else:
            return jsonify({'error': 'No allowed fields were modified'}), 400
            
    # Manager permissions - full access
    elif current_user.role == 'manager':
        # Manager can update all fields at any time
        task.customer_name = data.get('customer_name', task.customer_name)
        task.contact_number = data.get('contact_number', task.contact_number)
        task.service_type = data.get('service_type', task.service_type)
        task.assigned_to = data.get('assigned_to', task.assigned_to)
        task.branch_code = data.get('branch_code', task.branch_code)
        task.paymode = data.get('paymode', task.paymode)
        task.service_price = data.get('service_price', task.service_price)
        task.paid_amount = data.get('paid_amount', task.paid_amount)
        task.service_charge = data.get('service_charge', task.service_charge)
        task.description = data.get('description', task.description)
        task.status = data.get('status', task.status)
        task.edited = True
        task.edit_reason = data.get('edit_reason', 'Manager edit')
    
    # Admin permissions - full access
    else:
        task.customer_name = data.get('customer_name', task.customer_name)
        task.contact_number = data.get('contact_number', task.contact_number)
        task.service_type = data.get('service_type', task.service_type)
        task.assigned_to = data.get('assigned_to', task.assigned_to)
        task.branch_code = data.get('branch_code', task.branch_code)
        task.paymode = data.get('paymode', task.paymode)
        task.service_price = data.get('service_price', task.service_price)
        task.paid_amount = data.get('paid_amount', task.paid_amount)
        task.service_charge = data.get('service_charge', task.service_charge)
        task.description = data.get('description', task.description)
        task.status = data.get('status', task.status)
        task.edited = True
        task.edit_reason = data.get('edit_reason', 'Admin edit')

    # Handle payment completion when marking task as completed
    if data.get('status') == 'Completed' and task.get_due_amount() > 0:
        task.final_payment_amount = data.get('final_payment_amount', 0)
        task.final_paymode = data.get('final_paymode', 'Cash')
        task.payment_notes = data.get('payment_notes', '')
        # Update paid amount with final payment
        task.paid_amount += task.final_payment_amount

    try:
        db.session.commit()
        
        # Prepare task data for broadcasting
        task_data = {
            'id': task.id,
            'order_no': task.order_no,
            'customer_name': task.customer_name,
            'service_type': task.service_type,
            'status': task.status,
            'assigned_to': task.assigned_to,
            'branch_code': task.branch_code
        }
        
        # Broadcast task update
        broadcast_task_update('updated', task_data)
        # Broadcast dashboard update
        broadcast_dashboard_update()
        
        return jsonify({'success': True, 'message': 'Task updated successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to update task'}), 500

@app.route('/api/tasks/<int:task_id>', methods=['DELETE'])
@login_required
def delete_task(task_id):
    task = Task.query.get_or_404(task_id)
    
    # Check if task is completed
    is_completed = task.is_completed()

    # Staff permissions
    if current_user.role == 'staff':
        # Check if staff is allowed to delete this task
        if task.assigned_to != current_user.username and current_user.username not in task.get_shared_with():
            return jsonify({'error': 'You can only delete tasks assigned to you or shared with you'}), 403
        
        # Staff cannot delete completed orders
        if is_completed:
            return jsonify({'error': 'Cannot delete completed orders'}), 403

    # Manager and Admin can delete any task
    elif current_user.role not in ['manager', 'admin']:
        return jsonify({'error': 'Access denied'}), 403

    try:
        # Store task data before deletion for broadcasting
        task_data = {
            'id': task.id,
            'order_no': task.order_no
        }
        
        db.session.delete(task)
        db.session.commit()
        
        # Broadcast task deletion
        broadcast_task_update('deleted', task_data)
        # Broadcast dashboard update
        broadcast_dashboard_update()
        
        return jsonify({'success': True, 'message': 'Task deleted successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to delete task'}), 500

@app.route('/api/export-tasks')
@login_required
def export_tasks():
    """Export all tasks to Excel file with robust error handling"""
    tasks = None
    df = None
    output = None
    
    try:
        print("Export tasks endpoint called")
        print(f"Current user: {current_user.username}, Role: {current_user.role}")
        
        # ERROR HANDLING #1: Database Query with proper exception handling
        try:
            tasks = Task.query.order_by(Task.created_at.desc()).all()
            print(f"Found {len(tasks)} tasks")
        except Exception as db_error:
            print(f"Database query error: {db_error}")
            db.session.rollback()  # Rollback any failed transaction
            return jsonify({
                'error': 'Database error occurred while fetching tasks',
                'details': str(db_error)
            }), 500
        
        # Check if there are tasks
        if not tasks:
            print("No tasks to export")
            return jsonify({'error': 'No tasks to export'}), 400
        
        # ERROR HANDLING #2: Data preparation with safe attribute access
        tasks_data = []
        try:
            for task in tasks:
                try:
                    # Safe date formatting with fallback
                    completed_date = 'N/A'
                    if task.status == 'Completed' and task.updated_at:
                        try:
                            completed_date = task.updated_at.strftime('%Y-%m-%d %H:%M')
                        except (AttributeError, ValueError) as date_error:
                            print(f"Date formatting error for task {task.id}: {date_error}")
                            completed_date = 'N/A'
                    
                    # Safe numeric value extraction with type validation
                    def safe_float(value, default=0.0):
                        """Safely convert value to float, handling None, strings, and invalid types"""
                        if value is None:
                            return default
                        try:
                            return float(value)
                        except (ValueError, TypeError) as e:
                            print(f"Warning: Could not convert '{value}' to float: {e}")
                            return default
                    
                    revenue = safe_float(task.paid_amount)
                    service_price = safe_float(task.service_price)
                    paid_amount = safe_float(task.paid_amount)
                    service_charge = safe_float(task.service_charge)
                    final_payment = safe_float(task.final_payment_amount)
                    
                    # Calculate due amount safely
                    try:
                        due_amount = task.get_due_amount()
                        due_amount = safe_float(due_amount)
                    except Exception:
                        due_amount = safe_float(max(0, service_price - paid_amount))
                    
                    # Get shared with staff safely
                    def sanitize_text(text, max_length=32767):
                        """Sanitize text for Excel (max 32,767 chars per cell)"""
                        if text is None:
                            return 'N/A'
                        try:
                            # Convert to string and clean
                            text_str = str(text).strip()
                            # Replace problematic characters
                            text_str = text_str.replace('\x00', '').replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
                            # Truncate if too long
                            if len(text_str) > max_length:
                                text_str = text_str[:max_length-3] + '...'
                            return text_str if text_str else 'N/A'
                        except Exception:
                            return 'N/A'
                    
                    try:
                        shared_with = ', '.join(task.get_shared_with()) if task.get_shared_with() else 'None'
                        shared_with = sanitize_text(shared_with)
                    except Exception:
                        shared_with = 'None'
                    
                    # Safe task date formatting
                    task_date_str = 'N/A'
                    if task.task_date:
                        try:
                            task_date_str = task.task_date.strftime('%Y-%m-%d')
                        except (AttributeError, ValueError):
                            task_date_str = str(task.task_date) if task.task_date else 'N/A'
                    
                    # Safe created date formatting
                    created_date_str = 'N/A'
                    if task.created_at:
                        try:
                            created_date_str = task.created_at.strftime('%Y-%m-%d %H:%M')
                        except (AttributeError, ValueError):
                            created_date_str = str(task.created_at) if task.created_at else 'N/A'
                    
                    task_dict = {
                        # Keep Task ID as integer for proper Excel formatting
                        'Task ID': int(task.id) if task.id is not None else 0,
                        # All text fields sanitized
                        'Order No': sanitize_text(task.order_no),
                        'Customer Name': sanitize_text(task.customer_name),
                        'Contact Number': sanitize_text(task.contact_number),
                        'Service Type': sanitize_text(task.service_type),
                        'Description': sanitize_text(task.description),
                        'Status': sanitize_text(task.status),
                        'Assigned Staff': sanitize_text(task.assigned_to),
                        'Shared With': shared_with,  # Already sanitized above
                        'Branch': sanitize_text(task.branch_code),
                        'Payment Mode': sanitize_text(task.paymode),
                        # All numeric fields as float
                        'Service Price': service_price,
                        'Paid Amount': paid_amount,
                        'Service Charge': service_charge,
                        'Due Amount': due_amount,
                        'Revenue': revenue,
                        # Date strings
                        'Task Date': task_date_str,
                        'Created Date': created_date_str,
                        'Completed Date': completed_date,
                        # Boolean as text
                        'Edited': 'Yes' if task.edited else 'No',
                        # Text fields
                        'Edit Reason': sanitize_text(task.edit_reason),
                        'Final Payment Amount': final_payment,
                        'Final Payment Mode': sanitize_text(task.final_paymode),
                        'Payment Notes': sanitize_text(task.payment_notes)
                    }
                    tasks_data.append(task_dict)
                    
                except Exception as task_error:
                    # Log the error but continue processing other tasks
                    print(f"Error processing task {task.id if hasattr(task, 'id') else 'unknown'}: {task_error}")
                    continue
                    
        except Exception as data_prep_error:
            print(f"Data preparation error: {data_prep_error}")
            return jsonify({
                'error': 'Error preparing data for export',
                'details': str(data_prep_error)
            }), 500
        
        # Verify we have data to export
        if not tasks_data:
            return jsonify({'error': 'No valid task data could be prepared for export'}), 400
        
        # ERROR HANDLING #3: DataFrame creation and Excel writing
        try:
            # Create DataFrame
            df = pd.DataFrame(tasks_data)
            print(f"DataFrame created with {len(df)} rows and {len(df.columns)} columns")
            
        except Exception as df_error:
            print(f"DataFrame creation error: {df_error}")
            return jsonify({
                'error': 'Error creating data structure for Excel',
                'details': str(df_error)
            }), 500
        
        # Create Excel file with proper error handling
        try:
            # Create Excel file in memory
            output = io.BytesIO()
            
            # Use ExcelWriter with openpyxl engine
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Tasks')
                
                # Get the worksheet to format it
                try:
                    worksheet = writer.sheets['Tasks']
                    
                    # Auto-adjust column widths with error handling
                    for column in worksheet.columns:
                        max_length = 0
                        column_letter = column[0].column_letter
                        for cell in column:
                            try:
                                if cell.value is not None:
                                    cell_length = len(str(cell.value))
                                    if cell_length > max_length:
                                        max_length = cell_length
                            except Exception as cell_error:
                                # Skip problematic cells
                                print(f"Cell formatting warning: {cell_error}")
                                continue
                        
                        # Set column width with safety bounds
                        try:
                            adjusted_width = min(max(max_length + 2, 10), 50)
                            worksheet.column_dimensions[column_letter].width = adjusted_width
                        except Exception as width_error:
                            print(f"Column width adjustment warning: {width_error}")
                            continue
                            
                except Exception as format_error:
                    # Formatting is optional - log but don't fail
                    print(f"Excel formatting warning (non-critical): {format_error}")
            
            # Seek to beginning of file
            output.seek(0)
            print("Excel file created successfully in memory")
            
        except ImportError as import_error:
            print(f"Import error - missing dependency: {import_error}")
            return jsonify({
                'error': 'Excel export library not available',
                'details': 'openpyxl or pandas library may be missing or incompatible'
            }), 500
            
        except Exception as excel_error:
            print(f"Excel creation error: {excel_error}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")
            return jsonify({
                'error': 'Error generating Excel file',
                'details': str(excel_error)
            }), 500
        
        # Generate filename with current date
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f'tasks_export_{timestamp}.xlsx'
        except Exception:
            filename = 'tasks_export.xlsx'
        
        # Return file for download
        try:
            return send_file(
                output,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                as_attachment=True,
                download_name=filename
            )
        except Exception as send_error:
            print(f"File send error: {send_error}")
            return jsonify({
                'error': 'Error sending file to client',
                'details': str(send_error)
            }), 500
            
    except Exception as e:
        # Catch-all for any unexpected errors
        import traceback
        error_trace = traceback.format_exc()
        print(f"Unexpected export error: {e}")
        print(f"Traceback: {error_trace}")
        return jsonify({
            'error': 'An unexpected error occurred during export',
            'details': str(e)
        }), 500

# Complete task with payment endpoint
@app.route('/api/tasks/<int:task_id>/complete', methods=['POST'])
@login_required
def complete_task(task_id):
    task = Task.query.get_or_404(task_id)
    
    # Check permissions
    if current_user.role == 'staff':
        if task.assigned_to != current_user.username and current_user.username not in task.get_shared_with():
            return jsonify({'error': 'You can only complete tasks assigned to you or shared with you'}), 403
    
    data = request.get_json()
    
    # Check if there's due amount
    due_amount = task.get_due_amount()
    
    if due_amount > 0:
        # Validate payment data
        final_payment = data.get('final_payment_amount', 0)
        final_paymode = data.get('final_paymode')
        payment_notes = data.get('payment_notes', '')
        
        if not final_paymode:
            return jsonify({'error': 'Payment mode is required when completing task with due amount'}), 400
        
        if final_payment <= 0:
            return jsonify({'error': 'Final payment amount must be greater than 0'}), 400
        
        # Update payment information
        task.final_payment_amount = final_payment
        task.final_paymode = final_paymode
        task.payment_notes = payment_notes
        task.paid_amount += final_payment
    
    # Mark task as completed
    task.status = 'Completed'
    task.edited = True
    task.edit_reason = f"Task completed by {current_user.username}"

    try:
        db.session.commit()
        
        # Broadcast task update
        task_data = {
            'id': task.id,
            'order_no': task.order_no,
            'status': task.status
        }
        broadcast_task_update('completed', task_data)
        broadcast_dashboard_update()
        
        return jsonify({'success': True, 'message': 'Task completed successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to complete task'}), 500

# Cancel task endpoint (soft delete for staff)
@app.route('/api/tasks/<int:task_id>/cancel', methods=['POST'])
@login_required
def cancel_task(task_id):
    task = Task.query.get_or_404(task_id)
    
    # Check if task is completed
    if task.is_completed() and current_user.role == 'staff':
        return jsonify({'error': 'Staff cannot cancel completed orders'}), 403
    
    # Check permissions
    if current_user.role == 'staff':
        if task.assigned_to != current_user.username and current_user.username not in task.get_shared_with():
            return jsonify({'error': 'You can only cancel tasks assigned to you or shared with you'}), 403

    # Update task status to cancelled
    task.status = 'Cancelled'
    task.edited = True
    task.edit_reason = f"Order cancelled by {current_user.username} ({current_user.role})"

    try:
        db.session.commit()
        
        # Broadcast task update
        task_data = {
            'id': task.id,
            'order_no': task.order_no,
            'status': task.status
        }
        broadcast_task_update('cancelled', task_data)
        broadcast_dashboard_update()
        
        return jsonify({'success': True, 'message': 'Task cancelled successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to cancel task'}), 500

# Reopen completed task (Manager only)
@app.route('/api/tasks/<int:task_id>/reopen', methods=['POST'])
@login_required
def reopen_task(task_id):
    if current_user.role not in ['manager', 'admin']:
        return jsonify({'error': 'Only managers and admins can reopen completed orders'}), 403
    
    task = Task.query.get_or_404(task_id)
    
    if not task.is_completed():
        return jsonify({'error': 'Task is not completed'}), 400

    # Reopen the task by changing status to In Progress
    old_status = task.status
    task.status = 'In Progress'
    task.edited = True
    task.edit_reason = f"Order reopened from {old_status} by {current_user.username}"

    try:
        db.session.commit()
        
        # Broadcast task update
        task_data = {
            'id': task.id,
            'order_no': task.order_no,
            'status': task.status
        }
        broadcast_task_update('reopened', task_data)
        broadcast_dashboard_update()
        
        return jsonify({'success': True, 'message': 'Task reopened successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to reopen task'}), 500

# Take Over Task API
@app.route('/api/tasks/<int:task_id>/takeover', methods=['POST'])
@login_required
def take_over_task(task_id):
    task = Task.query.get_or_404(task_id)
    
    # Check if task is completed - staff cannot take over completed tasks
    if task.is_completed() and current_user.role == 'staff':
        return jsonify({'error': 'Cannot take over completed orders'}), 403
    
    # Check if task is already taken over by current user
    current_shared_with = task.get_shared_with()
    
    if current_user.username in current_shared_with:
        return jsonify({'error': 'You have already taken over this task'}), 400
    
    # Add current user to shared_with list
    current_shared_with.append(current_user.username)
    task.set_shared_with(current_shared_with)
    
    # Update edit reason
    task.edited = True
    task.edit_reason = f"Task taken over by {current_user.username}"
    
    try:
        db.session.commit()
        
        # Broadcast task update
        task_data = {
            'id': task.id,
            'order_no': task.order_no,
            'customer_name': task.customer_name,
            'service_type': task.service_type,
            'status': task.status,
            'assigned_to': task.assigned_to,
            'shared_with': task.get_shared_with()
        }
        broadcast_task_update('taken_over', task_data)
        
        return jsonify({
            'success': True, 
            'message': f'Task {task.order_no} taken over successfully'
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Failed to take over task'}), 500

@app.route('/api/tasks/<int:task_id>/share', methods=['POST'])
@login_required
def share_task(task_id):
    task = Task.query.get_or_404(task_id)
    
    # Check if task is completed - staff cannot share completed tasks
    if task.is_completed() and current_user.role == 'staff':
        return jsonify({'error': 'Cannot share completed orders'}), 403
    
    data = request.get_json()
    staff_to_share = data.get('staff_name')

    if not staff_to_share:
        return jsonify({'error': 'Staff name is required'}), 400

    # Verify staff exists
    staff_user = User.query.filter_by(username=staff_to_share, role='staff').first()
    if not staff_user:
        return jsonify({'error': 'Staff user not found'}), 404

    shared_with = task.get_shared_with()
    if not isinstance(shared_with, list):
        shared_with = []
    
    if staff_to_share not in shared_with:
        shared_with.append(staff_to_share)
        task.set_shared_with(shared_with)

        try:
            db.session.commit()
            
            # Broadcast task update for sharing
            task_data = {
                'id': task.id,
                'order_no': task.order_no,
                'shared_with': task.get_shared_with()
            }
            broadcast_task_update('shared', task_data)
            
            return jsonify({'success': True, 'message': f'Task shared with {staff_to_share}'})
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': 'Failed to share task'}), 500

    return jsonify({'success': True, 'message': 'Task already shared with this staff'})

# Dashboard data routes
@app.route('/api/dashboard/stats')
@login_required
def dashboard_stats():
    try:
        stats = get_dashboard_stats()
        return jsonify(stats)
    except Exception as e:
        return jsonify({'error': 'Database error'}), 500

@app.route('/api/dashboard/top-performers')
@login_required
def top_performers():
    try:
        # Get staff performance data
        staff_performance = []
        staff_users = User.query.filter_by(role='staff').all()

        for staff in staff_users:
            staff_tasks = Task.query.filter_by(assigned_to=staff.username).all()
            completed_tasks = len([t for t in staff_tasks if t.status == 'Completed'])
            total_revenue = sum(task.paid_amount for task in staff_tasks)
            score = completed_tasks * 10 + total_revenue / 100

            staff_performance.append({
                'name': staff.username,
                'completed_tasks': completed_tasks,
                'total_revenue': total_revenue,
                'score': score
            })

        # Sort by score and get top 3
        staff_performance.sort(key=lambda x: x['score'], reverse=True)
        top_performers = staff_performance[:3]

        return jsonify(top_performers)
    except Exception as e:
        return jsonify({'error': 'Database error'}), 500

@app.route('/api/dashboard/overdue-tasks')
@login_required
def overdue_tasks():
    try:
        overdue_threshold = datetime.now() - timedelta(hours=24)
        overdue_tasks = Task.query.filter(
            Task.status.in_(['Pending', 'In Progress', 'Hold']),
            Task.created_at < overdue_threshold
        ).all()

        tasks_data = []
        for task in overdue_tasks:
            hours_overdue = int((datetime.now() - task.created_at).total_seconds() / 3600)
            tasks_data.append({
                'order_no': task.order_no,
                'customer_name': task.customer_name,
                'service_type': task.service_type,
                'status': task.status,
                'assigned_to': task.assigned_to,
                'task_date': task.task_date.isoformat() if task.task_date else None,
                'hours_overdue': hours_overdue,
                'due_amount': task.get_due_amount()
            })

        return jsonify(tasks_data)
    except Exception as e:
        return jsonify({'error': 'Database error'}), 500

# Enhanced Reports API
@app.route('/api/reports/enhanced')
@login_required
def enhanced_reports():
    try:
        date_filter = request.args.get('date_filter', 'last30')
        
        # Base query
        query = Task.query
        
        # Apply date filter
        today = datetime.now().date()
        if date_filter == 'today':
            query = query.filter(Task.task_date == today)
        elif date_filter == 'yesterday':
            query = query.filter(Task.task_date == today - timedelta(days=1))
        elif date_filter == 'tomorrow':
            query = query.filter(Task.task_date == today + timedelta(days=1))
        elif date_filter == 'last30':
            query = query.filter(Task.task_date >= today - timedelta(days=30))
        # For 'all', no date filter applied
        
        tasks = query.all()
        
        # Calculate statistics
        total_tasks = len(tasks)
        completed_tasks = len([t for t in tasks if t.status == 'Completed'])
        pending_tasks = total_tasks - completed_tasks
        total_revenue = sum(task.paid_amount for task in tasks)
        
        # Branch breakdown
        branch_revenue = {}
        branches = ['BANK ROAD', 'UNIVERSITY ROAD']
        
        for branch in branches:
            branch_tasks = [t for t in tasks if t.branch_code == branch]
            branch_revenue[branch] = {
                'revenue': sum(t.paid_amount for t in branch_tasks),
                'tasks': len(branch_tasks),
                'completed': len([t for t in branch_tasks if t.status == 'Completed']),
                'pending': len([t for t in branch_tasks if t.status != 'Completed'])
            }
        
        # Staff performance (include all users who have tasks)
        staff_performance = {}
        all_users = User.query.all()
        
        for user in all_users:
            user_tasks = [t for t in tasks if t.assigned_to == user.username]
            if user_tasks:  # Only include users who have tasks
                completed = len([t for t in user_tasks if t.status == 'Completed'])
                revenue = sum(t.paid_amount for t in user_tasks)
                pending = len(user_tasks) - completed
                
                staff_performance[user.username] = {
                    'role': user.role,
                    'total_tasks': len(user_tasks),
                    'completed_tasks': completed,
                    'pending_tasks': pending,
                    'total_revenue': revenue,
                    'efficiency': round((completed / len(user_tasks)) * 100, 2) if user_tasks else 0
                }
        
        return jsonify({
            'summary': {
                'total_tasks': total_tasks,
                'completed_tasks': completed_tasks,
                'pending_tasks': pending_tasks,
                'total_revenue': total_revenue
            },
            'branch_revenue': branch_revenue,
            'staff_performance': staff_performance
        })
        
    except Exception as e:
        print(f"Error in enhanced reports: {e}")
        return jsonify({'error': str(e)}), 500

# Real-time refresh endpoint
@app.route('/api/refresh-data')
@login_required
def refresh_data():
    """Endpoint to manually trigger data refresh"""
    try:
        # Broadcast updates to all clients
        broadcast_dashboard_update()
        
        # Get latest tasks and broadcast
        tasks = Task.query.order_by(Task.created_at.desc()).limit(10).all()
        tasks_data = []
        for task in tasks:
            tasks_data.append({
                'id': task.id,
                'order_no': task.order_no,
                'customer_name': task.customer_name,
                'service_type': task.service_type,
                'status': task.status,
                'assigned_to': task.assigned_to
            })
        
        socketio.emit('tasks_refreshed', {'tasks': tasks_data})
        
        return jsonify({'success': True, 'message': 'Data refresh triggered'})
    except Exception as e:
        return jsonify({'error': 'Refresh failed'}), 500

# ==================== EXCEL IMPORT/EXPORT FUNCTIONALITY ====================

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def validate_task_row(row_data, row_num):
    """Validate a single task row from Excel"""
    errors = []
    
    # Required fields
    required_fields = ['customer_name', 'contact_number', 'service_type', 'assigned_to', 'branch_code']
    for field in required_fields:
        if field not in row_data or pd.isna(row_data[field]) or str(row_data[field]).strip() == '':
            errors.append(f"Row {row_num}: Missing required field '{field}'")
    
    # Validate branch code
    valid_branches = ['BANK ROAD', 'UNIVERSITY ROAD']
    if 'branch_code' in row_data and not pd.isna(row_data['branch_code']):
        if str(row_data['branch_code']).upper() not in valid_branches:
            errors.append(f"Row {row_num}: Invalid branch code. Must be one of: {', '.join(valid_branches)}")
    
    # Validate assigned_to exists
    if 'assigned_to' in row_data and not pd.isna(row_data['assigned_to']):
        user = User.query.filter_by(username=str(row_data['assigned_to'])).first()
        if not user:
            errors.append(f"Row {row_num}: User '{row_data['assigned_to']}' does not exist")
    
    # Validate status
    valid_statuses = ['Received', 'Pending', 'In Progress', 'Completed', 'Hold', 'Cancelled']
    if 'status' in row_data and not pd.isna(row_data['status']):
        if str(row_data['status']) not in valid_statuses:
            errors.append(f"Row {row_num}: Invalid status. Must be one of: {', '.join(valid_statuses)}")
    
    # Validate numeric fields
    numeric_fields = ['service_price', 'paid_amount', 'service_charge']
    for field in numeric_fields:
        if field in row_data and not pd.isna(row_data[field]):
            try:
                float(row_data[field])
            except (ValueError, TypeError):
                errors.append(f"Row {row_num}: '{field}' must be a number")
    
    return errors

def validate_service_row(row_data, row_num):
    """Validate a single service row from Excel"""
    errors = []
    
    # Required field
    if 'name' not in row_data or pd.isna(row_data['name']) or str(row_data['name']).strip() == '':
        errors.append(f"Row {row_num}: Missing required field 'name'")
    
    # Validate numeric fields
    numeric_fields = ['price', 'fee', 'charge']
    for field in numeric_fields:
        if field in row_data and not pd.isna(row_data[field]):
            try:
                float(row_data[field])
            except (ValueError, TypeError):
                errors.append(f"Row {row_num}: '{field}' must be a number")
    
    return errors

def validate_user_row(row_data, row_num):
    """Validate a single user row from Excel"""
    errors = []
    
    # Required fields
    required_fields = ['username', 'role']
    for field in required_fields:
        if field not in row_data or pd.isna(row_data[field]) or str(row_data[field]).strip() == '':
            errors.append(f"Row {row_num}: Missing required field '{field}'")
    
    # Validate role
    valid_roles = ['admin', 'manager', 'staff']
    if 'role' in row_data and not pd.isna(row_data['role']):
        if str(row_data['role']).lower() not in valid_roles:
            errors.append(f"Row {row_num}: Invalid role. Must be one of: {', '.join(valid_roles)}")
    
    return errors

@app.route('/api/import/excel', methods=['POST'])
@login_required
def import_excel():
    """Import data from Excel file"""
    # Check permissions - only admin and manager can import
    if current_user.role not in ['admin', 'manager']:
        return jsonify({'error': 'Access denied. Only admin and manager can import data'}), 403
    
    # Check if file is present
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'error': 'Invalid file type. Only .xlsx and .xls files are allowed'}), 400
    
    try:
        # Read Excel file
        excel_file = pd.ExcelFile(file)
        
        import_results = {
            'tasks': {'created': 0, 'updated': 0, 'errors': []},
            'services': {'created': 0, 'updated': 0, 'errors': []},
            'users': {'created': 0, 'updated': 0, 'errors': []}
        }
        
        # Process Tasks sheet
        if 'Tasks' in excel_file.sheet_names:
            df_tasks = pd.read_excel(excel_file, 'Tasks')
            
            for idx, row in df_tasks.iterrows():
                row_num = idx + 2  # Excel row number (header is row 1)
                
                # Validate row
                validation_errors = validate_task_row(row.to_dict(), row_num)
                if validation_errors:
                    import_results['tasks']['errors'].extend(validation_errors)
                    continue
                
                try:
                    # Check if task exists by ID or order_no
                    task = None
                    if 'id' in row and not pd.isna(row['id']):
                        task = Task.query.get(int(row['id']))
                    elif 'order_no' in row and not pd.isna(row['order_no']):
                        task = Task.query.filter_by(order_no=str(row['order_no'])).first()
                    
                    task_date = datetime.now().date()
                    if 'task_date' in row and not pd.isna(row['task_date']):
                        try:
                            if isinstance(row['task_date'], str):
                                task_date = datetime.strptime(row['task_date'], '%Y-%m-%d').date()
                            else:
                                task_date = row['task_date'].date() if hasattr(row['task_date'], 'date') else task_date
                        except:
                            pass
                    
                    if task:
                        # Update existing task
                        task.customer_name = str(row.get('customer_name', task.customer_name))
                        task.contact_number = str(row.get('contact_number', task.contact_number))
                        task.service_type = str(row.get('service_type', task.service_type))
                        task.status = str(row.get('status', task.status)) if 'status' in row and not pd.isna(row['status']) else task.status
                        task.assigned_to = str(row.get('assigned_to', task.assigned_to))
                        task.branch_code = str(row.get('branch_code', task.branch_code))
                        task.paymode = str(row.get('paymode', task.paymode)) if 'paymode' in row and not pd.isna(row['paymode']) else task.paymode
                        task.service_price = float(row.get('service_price', task.service_price)) if 'service_price' in row and not pd.isna(row['service_price']) else task.service_price
                        task.paid_amount = float(row.get('paid_amount', task.paid_amount)) if 'paid_amount' in row and not pd.isna(row['paid_amount']) else task.paid_amount
                        task.service_charge = float(row.get('service_charge', task.service_charge)) if 'service_charge' in row and not pd.isna(row['service_charge']) else task.service_charge
                        task.description = str(row.get('description', task.description)) if 'description' in row and not pd.isna(row['description']) else task.description
                        task.task_date = task_date
                        import_results['tasks']['updated'] += 1
                    else:
                        # Create new task
                        new_task = Task(
                            order_no=generate_order_no(str(row['branch_code']).upper()),
                            customer_name=str(row['customer_name']),
                            contact_number=str(row['contact_number']),
                            service_type=str(row['service_type']),
                            status=str(row.get('status', 'Received')),
                            assigned_to=str(row['assigned_to']),
                            branch_code=str(row['branch_code']).upper(),
                            paymode=str(row.get('paymode', 'Cash')),
                            service_price=float(row.get('service_price', 0)) if 'service_price' in row and not pd.isna(row['service_price']) else 0,
                            paid_amount=float(row.get('paid_amount', 0)) if 'paid_amount' in row and not pd.isna(row['paid_amount']) else 0,
                            service_charge=float(row.get('service_charge', 0)) if 'service_charge' in row and not pd.isna(row['service_charge']) else 0,
                            description=str(row.get('description', '')) if 'description' in row and not pd.isna(row['description']) else '',
                            task_date=task_date
                        )
                        db.session.add(new_task)
                        import_results['tasks']['created'] += 1
                
                except Exception as e:
                    import_results['tasks']['errors'].append(f"Row {row_num}: {str(e)}")
        
        # Process Services sheet
        if 'Services' in excel_file.sheet_names:
            df_services = pd.read_excel(excel_file, 'Services')
            
            for idx, row in df_services.iterrows():
                row_num = idx + 2
                
                # Validate row
                validation_errors = validate_service_row(row.to_dict(), row_num)
                if validation_errors:
                    import_results['services']['errors'].extend(validation_errors)
                    continue
                
                try:
                    # Check if service exists
                    service = None
                    if 'id' in row and not pd.isna(row['id']):
                        service = Service.query.get(int(row['id']))
                    elif 'name' in row and not pd.isna(row['name']):
                        service = Service.query.filter_by(name=str(row['name'])).first()
                    
                    if service:
                        # Update existing service
                        service.name = str(row.get('name', service.name))
                        service.price = float(row.get('price', service.price)) if 'price' in row and not pd.isna(row['price']) else service.price
                        service.fee = float(row.get('fee', service.fee)) if 'fee' in row and not pd.isna(row['fee']) else service.fee
                        service.charge = float(row.get('charge', service.charge)) if 'charge' in row and not pd.isna(row['charge']) else service.charge
                        service.link = str(row.get('link', service.link)) if 'link' in row and not pd.isna(row['link']) else service.link
                        service.note = str(row.get('note', service.note)) if 'note' in row and not pd.isna(row['note']) else service.note
                        import_results['services']['updated'] += 1
                    else:
                        # Create new service
                        new_service = Service(
                            name=str(row['name']),
                            price=float(row.get('price', 0)) if 'price' in row and not pd.isna(row['price']) else 0,
                            fee=float(row.get('fee', 0)) if 'fee' in row and not pd.isna(row['fee']) else 0,
                            charge=float(row.get('charge', 0)) if 'charge' in row and not pd.isna(row['charge']) else 0,
                            link=str(row.get('link', '')) if 'link' in row and not pd.isna(row['link']) else '',
                            note=str(row.get('note', '')) if 'note' in row and not pd.isna(row['note']) else ''
                        )
                        db.session.add(new_service)
                        import_results['services']['created'] += 1
                
                except Exception as e:
                    import_results['services']['errors'].append(f"Row {row_num}: {str(e)}")
        
        # Process Users sheet (admin only)
        if 'Users' in excel_file.sheet_names and current_user.role == 'admin':
            df_users = pd.read_excel(excel_file, 'Users')
            
            for idx, row in df_users.iterrows():
                row_num = idx + 2
                
                # Validate row
                validation_errors = validate_user_row(row.to_dict(), row_num)
                if validation_errors:
                    import_results['users']['errors'].extend(validation_errors)
                    continue
                
                try:
                    # Check if user exists
                    user = None
                    if 'id' in row and not pd.isna(row['id']):
                        user = User.query.get(int(row['id']))
                    elif 'username' in row and not pd.isna(row['username']):
                        user = User.query.filter_by(username=str(row['username'])).first()
                    
                    if user:
                        # Update existing user
                        user.email = str(row.get('email', user.email)) if 'email' in row and not pd.isna(row['email']) else user.email
                        user.role = str(row.get('role', user.role)).lower()
                        # Only update password if provided
                        if 'password' in row and not pd.isna(row['password']) and str(row['password']).strip():
                            user.set_password(str(row['password']))
                        import_results['users']['updated'] += 1
                    else:
                        # Create new user
                        new_user = User(
                            username=str(row['username']),
                            role=str(row['role']).lower(),
                            email=str(row.get('email', '')) if 'email' in row and not pd.isna(row['email']) else ''
                        )
                        # Set password (default or provided)
                        password = str(row.get('password', 'password123'))
                        new_user.set_password(password)
                        db.session.add(new_user)
                        import_results['users']['created'] += 1
                
                except Exception as e:
                    import_results['users']['errors'].append(f"Row {row_num}: {str(e)}")
        
        # Commit all changes
        db.session.commit()
        
        # Broadcast updates
        broadcast_dashboard_update()
        socketio.emit('data_imported', {'message': 'Data imported successfully'})
        
        # Prepare summary
        summary = {
            'success': True,
            'message': 'Import completed',
            'results': import_results,
            'summary': {
                'tasks': f"Created: {import_results['tasks']['created']}, Updated: {import_results['tasks']['updated']}, Errors: {len(import_results['tasks']['errors'])}",
                'services': f"Created: {import_results['services']['created']}, Updated: {import_results['services']['updated']}, Errors: {len(import_results['services']['errors'])}",
                'users': f"Created: {import_results['users']['created']}, Updated: {import_results['users']['updated']}, Errors: {len(import_results['users']['errors'])}"
            }
        }
        
        return jsonify(summary)
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Import failed: {str(e)}'}), 500

@app.route('/api/export/excel/<data_type>')
@login_required
def export_excel(data_type):
    """Export data to Excel file"""
    try:
        output = io.BytesIO()
        
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            if data_type == 'tasks' or data_type == 'all':
                # Export tasks
                tasks = Task.query.all()
                tasks_data = []
                for task in tasks:
                    tasks_data.append({
                        'id': task.id,
                        'order_no': task.order_no,
                        'customer_name': task.customer_name,
                        'contact_number': task.contact_number,
                        'service_type': task.service_type,
                        'status': task.status,
                        'assigned_to': task.assigned_to,
                        'branch_code': task.branch_code,
                        'paymode': task.paymode,
                        'service_price': task.service_price,
                        'paid_amount': task.paid_amount,
                        'service_charge': task.service_charge,
                        'description': task.description,
                        'task_date': task.task_date.isoformat() if task.task_date else '',
                        'created_at': task.created_at.isoformat() if task.created_at else ''
                    })
                df_tasks = pd.DataFrame(tasks_data)
                df_tasks.to_excel(writer, sheet_name='Tasks', index=False)
            
            if data_type == 'services' or data_type == 'all':
                # Export services
                services = Service.query.all()
                services_data = []
                for service in services:
                    services_data.append({
                        'id': service.id,
                        'name': service.name,
                        'price': service.price,
                        'fee': service.fee,
                        'charge': service.charge,
                        'link': service.link,
                        'note': service.note
                    })
                df_services = pd.DataFrame(services_data)
                df_services.to_excel(writer, sheet_name='Services', index=False)
            
            if (data_type == 'users' or data_type == 'all') and current_user.role == 'admin':
                # Export users (admin only)
                users = User.query.all()
                users_data = []
                for user in users:
                    users_data.append({
                        'id': user.id,
                        'username': user.username,
                        'email': user.email,
                        'role': user.role
                    })
                df_users = pd.DataFrame(users_data)
                df_users.to_excel(writer, sheet_name='Users', index=False)
        
        output.seek(0)
        
        filename = f'CRM_Export_{data_type}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )
    
    except Exception as e:
        return jsonify({'error': f'Export failed: {str(e)}'}), 500

@app.route('/api/download/template')
@login_required
def download_template():
    """Download Excel import template"""
    try:
        output = io.BytesIO()
        
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            # Tasks template
            tasks_template = pd.DataFrame({
                'customer_name': ['John Doe'],
                'contact_number': ['555-1234'],
                'service_type': ['Consultation'],
                'assigned_to': ['staff1'],
                'branch_code': ['BANK ROAD'],
                'status': ['Received'],
                'paymode': ['Cash'],
                'service_price': [1500],
                'paid_amount': [1000],
                'service_charge': [100],
                'description': ['Initial consultation'],
                'task_date': [datetime.now().strftime('%Y-%m-%d')]
            })
            tasks_template.to_excel(writer, sheet_name='Tasks', index=False)
            
            # Services template
            services_template = pd.DataFrame({
                'name': ['New Service'],
                'price': [1000],
                'fee': [50],
                'charge': [50],
                'link': ['https://example.com'],
                'note': ['Service description here']
            })
            services_template.to_excel(writer, sheet_name='Services', index=False)
            
            # Users template (admin only)
            if current_user.role == 'admin':
                users_template = pd.DataFrame({
                    'username': ['newuser'],
                    'email': ['newuser@example.com'],
                    'role': ['staff'],
                    'password': ['password123']
                })
                users_template.to_excel(writer, sheet_name='Users', index=False)
        
        output.seek(0)
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name='CRM_Import_Template.xlsx'
        )
    
    except Exception as e:
        return jsonify({'error': f'Template download failed: {str(e)}'}), 500

# ==================== END EXCEL IMPORT/EXPORT FUNCTIONALITY ====================

# Main route
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/health')
def health_check():
    try:
        # Test database connection
        User.query.first()
        return jsonify({'status': 'healthy', 'database': 'connected'})
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'database': 'disconnected', 'error': str(e)}), 500

if __name__ == '__main__':
    print("TaskFlow Application Started!")
    print("Default login credentials:")
    print("Admin: admin / admin123")
    print("Manager: manager / manager123") 
    print("Staff: staff1 / password123")
    print("Staff: staff2 / password123")
    print("Staff: staff3 / password123")
    print("\nNew Features:")
    print("- Admin can change staff passwords and usernames")
    print("- Staff Panel shows all tasks to everyone")
    print("- Overdue tasks tracking in dashboard")
    print("- Payment validation when completing tasks")
    print("- Enhanced reports with staff performance")
    print("\nAccess the application at: http://localhost:5000")
    print("Real-time updates enabled via WebSocket")
    socketio.run(app, debug=True, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)