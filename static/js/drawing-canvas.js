/**
 * Drawing Canvas for Student Writing/Equations
 * Touch-optimized for iPad with pressure sensitivity
 */

class DrawingCanvas {
    constructor(canvasId, options = {}) {
        this.canvas = document.getElementById(canvasId);
        if (!this.canvas) return;
        this.ctx = this.canvas.getContext('2d');

        this.options = {
            lineColor: options.lineColor || '#1e293b',
            lineWidth: options.lineWidth || 3,
            onSubmit: options.onSubmit || (() => {}),
            ...options
        };

        this.isDrawing = false;
        this.lastPoint = null;

        this.init();
    }

    init() {
        this.resize();
        window.addEventListener('resize', () => this.resize());

        this.ctx.lineCap = 'round';
        this.ctx.lineJoin = 'round';
        this.ctx.strokeStyle = this.options.lineColor;
        this.ctx.lineWidth = this.options.lineWidth;

        this.clear();

        this.canvas.addEventListener('mousedown', this.startDrawing.bind(this));
        this.canvas.addEventListener('mousemove', this.draw.bind(this));
        this.canvas.addEventListener('mouseup', this.stopDrawing.bind(this));
        this.canvas.addEventListener('mouseout', this.stopDrawing.bind(this));

        this.canvas.addEventListener('touchstart', this.handleTouchStart.bind(this));
        this.canvas.addEventListener('touchmove', this.handleTouchMove.bind(this));
        this.canvas.addEventListener('touchend', this.stopDrawing.bind(this));

        this.canvas.addEventListener('touchmove', (e) => e.preventDefault(), { passive: false });
    }

    resize() {
        const container = this.canvas.parentElement;
        if (!container) return;
        const rect = container.getBoundingClientRect();
        const imageData = this.ctx.getImageData(0, 0, this.canvas.width, this.canvas.height);
        this.canvas.width = rect.width;
        this.canvas.height = Math.max(300, rect.height);
        this.ctx.lineCap = 'round';
        this.ctx.lineJoin = 'round';
        this.ctx.strokeStyle = this.options.lineColor;
        this.ctx.lineWidth = this.options.lineWidth;
        this.ctx.putImageData(imageData, 0, 0);
    }

    getCanvasCoordinates(event) {
        const rect = this.canvas.getBoundingClientRect();
        const scaleX = this.canvas.width / rect.width;
        const scaleY = this.canvas.height / rect.height;
        return {
            x: (event.clientX - rect.left) * scaleX,
            y: (event.clientY - rect.top) * scaleY
        };
    }

    getTouchCoordinates(touch) {
        const rect = this.canvas.getBoundingClientRect();
        const scaleX = this.canvas.width / rect.width;
        const scaleY = this.canvas.height / rect.height;
        return {
            x: (touch.clientX - rect.left) * scaleX,
            y: (touch.clientY - rect.top) * scaleY
        };
    }

    startDrawing(event) {
        this.isDrawing = true;
        this.lastPoint = this.getCanvasCoordinates(event);
    }

    handleTouchStart(event) {
        event.preventDefault();
        if (event.touches.length === 1) {
            this.isDrawing = true;
            this.lastPoint = this.getTouchCoordinates(event.touches[0]);
            if (event.touches[0].force) {
                this.ctx.lineWidth = this.options.lineWidth * event.touches[0].force * 2;
            }
        }
    }

    draw(event) {
        if (!this.isDrawing) return;
        const currentPoint = this.getCanvasCoordinates(event);
        this.drawLine(this.lastPoint, currentPoint);
        this.lastPoint = currentPoint;
    }

    handleTouchMove(event) {
        event.preventDefault();
        if (!this.isDrawing || event.touches.length !== 1) return;
        const currentPoint = this.getTouchCoordinates(event.touches[0]);
        if (event.touches[0].force) {
            this.ctx.lineWidth = this.options.lineWidth * event.touches[0].force * 2;
        }
        this.drawLine(this.lastPoint, currentPoint);
        this.lastPoint = currentPoint;
    }

    drawLine(from, to) {
        this.ctx.beginPath();
        this.ctx.moveTo(from.x, from.y);
        this.ctx.lineTo(to.x, to.y);
        this.ctx.stroke();
    }

    stopDrawing() {
        this.isDrawing = false;
        this.lastPoint = null;
    }

    clear() {
        this.ctx.fillStyle = '#ffffff';
        this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
    }

    getImageData() {
        return this.canvas.toDataURL('image/png');
    }

    submit() {
        this.options.onSubmit(this.getImageData());
    }

    setLineColor(color) {
        this.options.lineColor = color;
        this.ctx.strokeStyle = color;
    }

    setLineWidth(width) {
        this.options.lineWidth = width;
        this.ctx.lineWidth = width;
    }
}

window.DrawingCanvas = DrawingCanvas;
